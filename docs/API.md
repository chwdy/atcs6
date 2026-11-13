# ATCS6 REST API Documentation

Base URL: `http://localhost:5007`

This file describes the requests the Editor screen uses to draw and edit tiles,
segments, blocks, switches and tunnels, the snapshot the page reads, and the
layout save and load requests. The rules these requests must keep are in
design_v2.md under **Tile**, **Tunnel**, **Segment**, **Block** and **Switch**.

The other requests are described in design_v2.md, not here:

- train requests: **Running a train** and **Trains** (see also
  [Train requests](#train-requests) below);
- hardware, Setup and activation requests: **Hardware REST API** (see also
  [Hardware requests](#hardware-requests) below);
- `GET` / `PUT /api/settings`: **Runtime settings**;
- `GET /api/event-log`: **Event log**.

## Contents

- [Page and static files](#page-and-static-files)
- [Snapshot](#snapshot)
- [Tiles](#tiles)
- [Segments](#segments)
- [Blocks](#blocks)
- [Switches](#switches)
- [Tunnels](#tunnels)
- [Layouts](#layouts)
- [Server](#server)
- [Train requests](#train-requests)
- [Hardware requests](#hardware-requests)
- [Error responses](#error-responses)

---

## Page and static files

### GET /

Serves `static/index.html`.

### GET /<path:path>

Serves a file from the `static/` directory (for example `GET /dist/main.js`).
A path with no file extension that matches no file is answered with
`index.html`, so the page's own router can handle it. A missing file that has
an extension is a real `404`. Every reply carries `Cache-Control: no-cache`.

---

## Snapshot

### GET /api/network

Returns the snapshot: the layout and its live state (design_v2.md **Data flow
to the page**).

**Query parameter:** `layout_version` (optional). Every reply carries
`layout_version`, a hash of the layout part of the snapshot. When the request
passes the current value, the reply leaves the layout out: it carries
`layout_unchanged: true` and `live_blocks`, `live_switches` and `live_tracks`
(only the fields that change during a run) instead of `tracks`, `blocks`,
`switches` and `tunnels`. Otherwise the reply is the full snapshot with
`layout_unchanged: false`. See design_v2.md **Layout version**,
`network_poll.py` and `frontend/src/lib/network-poll.ts`.

The page reads the snapshot from `GET /api/network/stream` while that stream is
open, and uses this request for one-off reads and while the stream is down.

**Response (full snapshot, abridged):**

```json
{
  "tracks": [{
    "x": 3, "y": 5,
    "track_bits": 4,
    "type": "diagonal",
    "edges": ["E", "N"],
    "dead_end_ports": [],
    "is_switch": false,
    "switch_id": null,
    "switch_state": null,
    "switch_states": null,
    "switch_points": {},
    "segment_id": "SEG_4F110A",
    "block_section_id": "BLK_C37589A5",
    "length": 1.0,
    "is_valid": true,
    "num_paths": 1,
    "has_long_path": false,
    "has_short_path": true,
    "long_path_type": null,
    "is_diagonal_only_switch": false
  }],
  "blocks": {
    "BLK_4F4CF0": {
      "block_id": "BLK_4F4CF0",
      "name": "Right",
      "segment_ids": ["SEG_4F4CF0"],
      "segments": [{ "segment_id": "SEG_4F4CF0", "...": "segment fields" }],
      "ordered_tiles": [[9, 13, "W"], [10, 13, "W"]],
      "length_cm": 99.6,
      "min_radius_cm": null,
      "grade_percent": null,
      "speed_limit_kmh": null,
      "is_holding_point": false,
      "allowed_travel_directions": ["forward", "reverse"],
      "disabled": false,
      "manual": false,
      "current_voltage": 0.0,
      "state": "free",
      "reads_occupied": false,
      "train_id": null,
      "claimed_by_train_id": null,
      "voltage": 0.0
    }
  },
  "switches": {
    "SW_11_9": {
      "switch_id": "SW_11_9",
      "x": 11, "y": 9,
      "name": "",
      "notes": "",
      "switch_type": "standard",
      "main_length_cm": 0.0,
      "diverge_length_cm": 0.0,
      "main_radius_cm": null,
      "diverge_radius_cm": null,
      "position": 0,
      "position_known": true,
      "flip_count": 0,
      "disabled": false,
      "manual": false,
      "motor_unavailable": false,
      "allow_exit_from_either_leg": false,
      "held": false,
      "alternate_next": null
    }
  },
  "tunnels": [],
  "running_trains": {},
  "session_state": { "value": "halted", "halt_reason": "not_activated" },
  "operation_environment": "virtual_hardware",
  "operation_mode": "virtual_hardware",
  "environment_active": false,
  "activation_issues": [],
  "occupancy_is_real": false,
  "hardware_failure": false,
  "runtime_halted": false,
  "hardware_failure_detail": null,
  "layout_version": "c75b7ea48f9b82a3f086d24151cb1b87",
  "layout_unchanged": false
}
```

What the code sends, where it differs from design_v2.md **Live fields**:

- The train map is sent as `running_trains`, not `trains`. Each entry is the
  train entry **Live fields** describes.
- Each block also carries `state`: `"occupied"` when the block reads occupied
  or is one of a train's blocks, `"reserved"` when a train holds a claim on it,
  and `"free"` otherwise. A block whose `state` is `"occupied"` also carries
  `occupied_by`, the `train_id` of the train whose block it is, or `null`.
  These two are worked out in `add_train_runtime_fields` in `simulation.py`.
- Each block carries `current_voltage` as well as `voltage`; both are the
  voltage `BlockPowerController` last recorded for the block.
- `disabled` and `manual` on blocks and switches, and `flip_count`,
  `motor_unavailable` and `allow_exit_from_either_leg` on switches, are sent
  in the live part.

### GET /api/network/stream

The same snapshot, pushed. A server-sent event stream (`text/event-stream`):
each message is one `data:` line holding the JSON reply `GET /api/network`
would give at that moment. The server sends a message every 200 ms, and right
after any `POST`, `PUT`, `PATCH` or `DELETE` request finishes. It builds each
snapshot once for all open streams.

**Query parameter:** `layout_version` (optional). The first message leaves the
layout out only when this names the current version; each later message leaves
it out while the version equals the one the previous message carried. See
design_v2.md **Network stream**, `network_stream.py` and
`frontend/src/lib/network-stream.ts`.

---

## Tiles

A tile is one grid square of track (design_v2.md **Tile**). Tile coordinates in
`/api/track/...` may be negative: `/api/track/-5/10`.

### GET /api/track/{x}/{y}

Returns the tile at those coordinates, in the shape of one `tracks` entry of
the snapshot.

**Error (404):** `{"error": "Track not found"}`

### PATCH /api/track/{x}/{y}

Changes one tile.

**Request:**

```json
{
  "track_bits": 7,
  "dead_end_ports": ["N"],
  "switch_state": 0,
  "segment_id": "SEG_001",
  "force": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| track_bits | int | New tile geometry. A tile that becomes a switch leaves its segment, and a switch record is made for it. |
| dead_end_ports | array | Edges (`N`, `E`, `S`, `W`) that end the track. |
| switch_state | int | `0` = main, `1` = diverge. On a tile that holds a switch, sent to `SwitchManager.request_position_change` when the runtime is present. |
| segment_id | string | Sets the tile's `segment_id` directly. |
| force | bool | Confirms a change that would remove a switch. |

**Responses:**

- `200` `{"status": "success", "track": { ... }}`
- `200` `{"status": "success", "message": "Track at (x, y) removed (empty)"}`
  when the change leaves the tile with no track and no dead ends.
- `409` `{"requires_confirmation": true, "impact": { ... }}` when the change
  would remove a switch and `force` is not set.
- `409` `{"error": "<reason>"}` when the switch throw is refused.
- `400` `{"success": false, "error": "<message>"}` for track bits that are not
  allowed; `400` `{"error": ...}` for a malformed body, or when the change would
  empty a segment in the middle of its block.
- `404` `{"error": "Track not found"}`

### DELETE /api/track/{x}/{y}

Removes the tile.

**Response:** `{"status": "success", "message": "Track at (0, 0) removed"}`

**Errors:** `404` when there is no tile; `400` when removing it would empty a
segment in the middle of its block.

### POST /api/tracks/bulk

Adds many tiles at once (drag-to-build).

**Request:**

```json
{
  "tracks": [
    {"x": 0, "y": 0, "track_bits": 2},
    {"x": 1, "y": 0, "track_bits": 2}
  ]
}
```

**Response:**

```json
{
  "status": "success",
  "added": 2,
  "updated": 0,
  "tracks": [ ... ]
}
```

- Where a tile already exists, the new bits are merged into it (bitwise OR).
- Track bits that are not allowed are skipped without an error.
- Neighbouring tiles are updated to match.

### POST /api/tracks/bulk-delete

Removes many tiles at once.

**Request:**

```json
{
  "positions": [[0, 0], [0, 1], [0, 2]],
  "force": false
}
```

**Responses:**

- `200` `{"status": "success", "deleted": 3}`
- `409` `{"requires_confirmation": true, "impact": { ... }}` when the removal
  needs confirming and `force` is not set.
- `400` `{"error": ...}` for a malformed body, or when the removal would empty
  a segment in the middle of its block.

---

## Segments

A segment holds the geometry of a run of tiles: its tile order, length,
radius, grade and speed limit (design_v2.md **Segment**). Every segment belongs
to one block; a new segment gets a block of its own.

A segment reply is the segment's own fields plus `block_id` (the block it
belongs to), `track_count` and `track_positions`:

```json
{
  "segment_id": "SEG_D67B49B7",
  "block_id": "BLK_D67B49B7",
  "name": "",
  "ordered_tiles": [[3, 0, "W"], [4, 0, "W"], [5, 0, "W"]],
  "length_cm": 20,
  "min_radius_cm": 40.0,
  "grade_percent": null,
  "speed_limit_kmh": null,
  "track_count": 3,
  "track_positions": [[3, 0], [4, 0], [5, 0]]
}
```

### GET /api/segments

Lists every segment as a segment reply.

### GET /api/segments/{segment_id}

One segment reply, plus `is_continuous` (bool) and `validation_errors` (list).

**Error (404):** `{"error": "Segment not found"}`

### POST /api/segments

Makes a segment from the given tiles, and a block that holds only that segment.

**Request:**

```json
{
  "positions": [[0, 0], [1, 0], [2, 0]],
  "segment_id": "SEG_A",
  "name": "A",
  "length_cm": 30,
  "speed_limit_kmh": null
}
```

Only `positions` is required. `max_speed_kmh` is read when `speed_limit_kmh` is
absent.

**Response:** the new segment reply. **Error (400):** `{"error": ...}`.

### PATCH /api/segments/{segment_id}

Changes a segment's `name`, `length_cm`, `min_radius_cm`, `grade_percent` or
`speed_limit_kmh` (`max_speed_kmh` is read when `speed_limit_kmh` is absent).

**Response:** the segment reply. **Errors:** `404`, `400`.

### DELETE /api/segments/{segment_id}

Deletes the segment, and its block when it was the block's only segment. A
segment in the middle of a block of several segments is refused with `400`,
because deleting it would leave the block in two pieces.

**Response:** `{"status": "success", "message": "Segment SEG_A deleted"}`

### POST /api/segments/{segment_id}/reverse-orientation

Reverses the order of the segment's tiles (design_v2.md **Segment**).

**Response:** `{"status": "success", "segment_id": "SEG_A", "reversed": true}`

**Error (404):** `{"error": "Segment not found"}`

### POST /api/segments/{segment_id}/extend

Adds tiles to the segment.

**Request:** `{"positions": [[5, 0]], "force_overwrite": false}`.
`force_overwrite` moves a tile that already belongs to another segment.

**Response:** the segment reply. **Errors:** `400` (also when `positions` is
empty), `404`.

### POST /api/segments/{segment_id}/tracks

Assigns tiles to the segment by position.

**Request:** `{"positions": [[6, 0]], "force": false}`. A tile that belongs to
another segment is skipped unless `force` is set.

**Response:**

```json
{
  "status": "success",
  "segment_id": "SEG_A",
  "added": 1,
  "positions": [[6, 0]],
  "reassigned": 0,
  "reassignment_details": []
}
```

### DELETE /api/segments/{segment_id}/tracks

Takes tiles out of the segment.

**Request:** `{"positions": [[6, 0]]}`

**Response:**

```json
{
  "status": "success",
  "block_id": "BLK_A",
  "positions": [[6, 0]],
  "removed": 1,
  "segment_deleted": false,
  "errors": []
}
```

**Errors:** `404`, `400`.

### GET /api/segments/{segment_id}/end_tiles

The first and last tile of the segment, in track order.

**Response:** `{"start_tile": {"x": 9, "y": 13}, "end_tile": {"x": 12, "y": 9}}`
(both `null` when the segment has no tiles).

### POST /api/segments/{segment_id}/can_extend

Says whether a tile could be added to the segment, without changing anything.

**Request:** `{"x": 5, "y": 0}`

**Response:** `{"can_extend": true}`, or

```json
{
  "can_extend": false,
  "reason": "Tile already in segment SEG_B - use force_overwrite=true",
  "requires_force_overwrite": true
}
```

Other reasons: no track at the position, the tile is a crossing, the tile is a
switch, or the tile is not next to the segment.

---

## Blocks

A block groups one or more segments that share one power feed and one
occupancy detector (design_v2.md **Block**). Geometry is edited on the
segments; the block requests change grouping, name and the availability flags.

A block reply is the block's own fields plus `current_voltage`:

```json
{
  "block_id": "BLK_3E1C9417",
  "name": "Main",
  "segment_ids": ["SEG_201936B2", "SEG_D67B49B7"],
  "segments": [ { "...": "segment fields" } ],
  "ordered_tiles": [[0, 0, "W"], [1, 0, "W"]],
  "length_cm": 50,
  "min_radius_cm": 40.0,
  "grade_percent": null,
  "speed_limit_kmh": null,
  "is_holding_point": false,
  "allowed_travel_directions": ["forward", "reverse"],
  "disabled": false,
  "manual": false,
  "current_voltage": 0.0
}
```

### GET /api/blocks

Lists every block as a block reply plus `track_count` and `track_positions`.

### GET /api/blocks/{block_id}

One block reply plus `track_count`, `track_positions`, `sorted_positions`
(the tiles in track order), `is_continuous` and `validation_errors` (a block is
continuous when each of its segments is).

**Error (404):** `{"error": "Block not found"}`

### POST /api/blocks

Groups existing segments into one block.

**Request:**

```json
{
  "segment_ids": ["SEG_201936B2"],
  "block_id": "BLK_MAIN",
  "name": "Main"
}
```

Only `segment_ids` is required. Grouping never reverses a segment: a segment
that runs against the others is refused and named in the error (reverse it
first with `POST /api/segments/{segment_id}/reverse-orientation`).

**Response:** the block reply. **Error (400):** `{"error": ...}`.

### PATCH /api/blocks/{block_id}

Changes `name`, `is_holding_point`, `allowed_travel_directions` (a non-empty
list of `"forward"` / `"reverse"`), `disabled` or `manual`.

- A geometry field (`length_cm` and the like) is refused with `400`: edit it on
  the segment.
- `polarity_reversed` is refused with `400`: it is part of the block's
  `block_track` binding, set through `PUT /api/hardware/binding`.

**Response:** the block reply. **Errors:** `404`, `400`, `409`, `410`. Each
call also adds a `command_result` event (`source` `"Block"`, `command_name`
`"update_flags"`) to the event stream, on success and on a `409` refusal.

### DELETE /api/blocks/{block_id}

Ungroups the block. Each of its segments goes back to a block of its own; no
geometry is lost.

**Response:** `{"status": "success", "message": "Block BLK_A ungrouped"}`

### POST /api/blocks/{block_id}/segments

Adds a segment to either end of the block.

**Request:** `{"segment_id": "SEG_D67B49B7"}`

**Response:** the block reply. **Errors:** `400` (also when `segment_id` is
missing), `404`.

### DELETE /api/blocks/{block_id}/segments/{segment_id}

Takes a segment off either end of the block. The segment goes back to a block
of its own.

**Response:** the block reply, or, when that was the block's last segment and
the block is gone:

```json
{
  "status": "success",
  "block_dissolved": true,
  "message": "Block BLK_A dissolved (last member removed)"
}
```

**Errors:** `404`, `400`.

### GET /api/blocks/{block_id}/voltage

The block's current voltage, from `BlockPowerController`.

**Response:**

```json
{
  "block_id": "BLK_001",
  "voltage": 12.0,
  "timestamp": "2026-11-13T09:38:22.677203"
}
```

**Error (404):** `{"error": "Block 'BLK_001' not found"}`

---

## Switches

A switch record holds a switch tile's geometry, name and position
(design_v2.md **Switch**).

A switch reply:

```json
{
  "switch_id": "SW_11_9",
  "x": 11, "y": 9,
  "name": "",
  "notes": "",
  "switch_type": "standard",
  "main_length_cm": 0.0,
  "diverge_length_cm": 0.0,
  "main_radius_cm": null,
  "diverge_radius_cm": null,
  "position": 0,
  "position_known": true,
  "flip_count": 0,
  "disabled": false,
  "manual": false,
  "motor_unavailable": false,
  "allow_exit_from_either_leg": false
}
```

### GET /api/switches

Lists every switch reply.

### GET /api/switches/{switch_id}

One switch reply. **Error (404):** `{"error": "Switch not found"}`

### POST /api/tracks/{x}/{y}/switch

Makes the switch record for a switch tile. `x` and `y` must not be negative
here.

**Request (optional):** `name`, `notes`, `switch_type`, `main_length_cm`,
`diverge_length_cm`, `main_radius_cm`, `diverge_radius_cm`.

**Responses:** `201` with the new switch reply; `200` with the existing one
when the tile already has a switch; `404` when there is no tile; `400`
`{"error": "Track is not a switch"}`.

### PUT /api/switches/{switch_id}

Changes any of the switch's fields, including `disabled`, `manual`,
`motor_unavailable` and `allow_exit_from_either_leg`.

**Response:** the switch reply. **Errors:** `400` (also for an empty body),
`404`, `409` when a physical setting changes while the session is Live. Each
call also adds a `command_result` event (`source` `"Switch"`, `command_name`
`"update_flags"`) to the event stream, on success and on a `409` refusal.

### PATCH /api/switches/{switch_id}

The same as `PUT /api/switches/{switch_id}`.

### DELETE /api/switches/{switch_id}

Removes the switch record.

**Response:** `{"success": true}`. **Error (404).**

### PUT /api/switches/{switch_id}/state

The operator's request to throw a switch (design_v2.md **Running a train**,
Throw a switch, and **Switch**).

**Request:**

```json
{
  "state": 1,
  "command_id": "optional",
  "manual_override": false
}
```

**Responses:**

- `202` `{"command_id": "...", "status": "queued"}`. The result arrives later
  as a `command_result` event with `source` `"Switch"`.
- `409` `{"success": false, "reason": "switch_held", "error": "..."}` while
  the session is Live and the switch is a held switch.
- `400` for a missing or wrong `state`; `404` for an unknown switch; `503`
  when `SwitchManager` is not available.

### PUT /api/switches/{switch_id}/alternate

Turns alternate arrivals on or off (design_v2.md **Running a train**, Alternate
arrivals).

**Request:** `{"enabled": true}`

**Response:** `{"alternate_next": 0}`: the position (`0` main, `1` diverge)
the switch is set to for the next train, or `null` once turned off. **Errors:** `400` when `enabled` is not a bool; `404`; `409` while
the session is not Live, when the switch is disabled, manual or has no motor,
or when its two legs do not each lead to a block of their own.

---

## Tunnels

A tunnel joins two dead-end tile edges so a train passes from one to the other
(design_v2.md **Tunnel**).

### GET /api/tunnels

**Response:**

```json
[
  {"a": {"x": 0, "y": 0, "edge": "W"}, "b": {"x": 5, "y": 0, "edge": "E"}}
]
```

### POST /api/tunnels

Adds a tunnel.

**Request:** `{"a": {"x": 0, "y": 0, "edge": "W"}, "b": {"x": 5, "y": 0, "edge": "E"}}`

**Response (201):** the full tunnel list. **Error (400):** a malformed body or
a pair the network refuses.

### DELETE /api/tunnels

Removes the tunnel that has the given end.

**Request:** `{"x": 0, "y": 0, "edge": "W"}`

**Response:** the full tunnel list. **Errors:** `400` for a malformed body;
`404` `{"error": "No tunnel at that endpoint"}`.

---

## Layouts

Layouts are saved as JSON files in `layouts/`. The server also saves the
current layout every 5 minutes to `layouts/_autosave.json`. Each file holds a
snapshot (for loading), construction steps (for recovery when the snapshot
cannot be read) and the Hardware Setup. See `docs/PERSISTENCE_DESIGN.md`.

### GET /api/layouts

**Response:**

```json
{
  "layouts": [
    {
      "name": "My Layout",
      "filename": "my_layout",
      "description": "A sample layout",
      "modified": "2026-11-13T09:39:08.415239",
      "size_bytes": 2909,
      "has_snapshot": true,
      "has_steps": true,
      "has_hardware": true
    }
  ]
}
```

### POST /api/layouts

Saves the current layout.

**Request:** `{"name": "My Layout", "description": "A sample layout"}`
(`name` required).

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

When the steps could not be made, the reply also carries
`"warning": "Steps could not be generated: <message>"`.

**Errors:** `400` for a missing or bad name; `500` when the file cannot be
written.

### POST /api/layouts/{name}/load

Loads a layout in place of the current one. It also takes the layout's saved
Hardware Setup, which applies when the session is next built.

**Response:**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' loaded successfully",
  "method": "snapshot",
  "hardware": "applied"
}
```

- `method` is `"snapshot"`, or `"steps"` when the layout was rebuilt from its
  steps. In that case the reply also carries `"recovery": true` and the message
  says the layout was recovered from steps.
- `hardware` is `"applied"`, `"absent"` (the file has no Hardware Setup) or
  `"error"`. On `"error"` the reply also carries a `warning` naming what went
  wrong; the layout itself is still loaded.

**Errors:** `404`, `400`.

### DELETE /api/layouts/{name}

**Response:** `{"status": "success", "message": "Layout 'My Layout' deleted"}`

**Error (404):** `{"error": "Layout 'My Layout' not found"}`

### POST /api/network/clear

Clears the layout, giving an empty canvas. Every train is removed with it.

**Response:**

```json
{
  "status": "success",
  "message": "Network cleared",
  "cleared": {
    "tracks": 43,
    "blocks": 5,
    "trains": 0,
    "switches": 2,
    "tunnels": 0
  }
}
```

### GET /api/network/stats

Counts of what the layout holds, used to check whether it is empty.

**Response:**

```json
{
  "tracks": 43,
  "blocks": 5,
  "trains": 0,
  "switches": 2,
  "tunnels": 0,
  "is_empty": false
}
```

---

## Server

### GET /api/health

**Response:** `{"ok": true, "boot_id": "...", "pid": 12345, "uptime_seconds": 12.3}`.
`boot_id` changes each time the server process starts, so the page can tell a
dropped connection from a restarted server.

### GET /api/server/shutdown-report

How the previous server process ended: `{"previous": null}` on the first run,
otherwise `{"previous": { ... }}`, whose `clean` flag is `false` when the
process was killed before it could put the layout in the safe state.

### POST /api/server/restart

Puts the layout in the safe state, saves the layout to `_autosave`, and starts
a new server process in place of this one.

**Response:** `{"status": "restarting", "flushed": true, "flush_error": null}`

---

## Train requests

These are described in design_v2.md **Running a train** and **Trains**. The
routes that exist now (`routes/running_trains.py`):

- `PUT /api/trains/{train_id}/knob`
- `POST /api/trains/{train_id}/reverse`
- `POST /api/trains/{train_id}/stop`
- `POST /api/trains/stop-all`
- `DELETE /api/trains/{train_id}`
- `GET /api/trains/{train_id}/notifications` (design_v2.md **Runtime
  notifications**)

---

## Hardware requests

These are described in design_v2.md **Hardware REST API**. Routes in
`routes/hardware.py` that the table there does not list:

- `GET /api/hardware/coverage` — which blocks and switches in the loaded
  layout have no binding, and which bindings name blocks or switches the layout
  no longer has.
- `GET /api/hardware/occupancy/raw` — what the occupancy read saw on its last
  read, before debounce, with the USB link state. It reads recorded state only
  and sends nothing to the rig.
- `PUT /api/hardware/binding` — sets or clears one binding of one block or
  switch. Body `{map, logical_id, chip_id?, port?}`; no `chip_id` clears it.
- `GET /api/debug/hw`, `POST /api/debug/hw/occupancy`,
  `POST /api/debug/hw/current` — other names for `GET /api/virtual-hardware`,
  `PUT /api/virtual-hardware/input/occupancy` and
  `PUT /api/virtual-hardware/input/current`.

---

## Error responses

Errors return JSON with an `error` field:

```json
{
  "error": "Error message"
}
```

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad request (malformed body, a value that is not allowed, an edit the layout rules refuse) |
| 404 | The tile, segment, block, switch, tunnel or layout named does not exist |
| 409 | The request needs confirming (`requires_confirmation`), or is refused in the current session state |
| 410 | The block is gone |
| 500 | Server error |
| 503 | A part of the runtime the request needs is not available |
