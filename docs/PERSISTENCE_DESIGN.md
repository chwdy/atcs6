# Persistence Design

## Overview

The system uses JSON file persistence with no database. All runtime data lives in-memory in the `RailwayNetwork` object. Persistence is handled by `PersistenceManager` (`persistence.py`) which saves and loads network state to/from JSON files in the `layouts/` directory.

## File Format

Each layout file uses a **merged format** containing two representations of the same network:

```json
{
  "snapshot": { ... },
  "steps": [ ... ],
  "metadata": {
    "name": "My Layout",
    "description": "Optional description",
    "saved_at": "2026-01-15T10:30:00",
    "track_count": 42,
    "block_count": 5,
    "signal_count": 8,
    "station_count": 3,
    "train_count": 2
  }
}
```

### Snapshot (Primary)

The `snapshot` object is a complete serialization of the `RailwayNetwork` state:

```json
{
  "snapshot": {
    "tracks": {
      "0,0": { "x": 0, "y": 0, "track_bits": 3, "block_section_id": "BLK_abc", ... },
      "1,0": { ... }
    },
    "blocks": {
      "BLK_abc": { "block_id": "BLK_abc", "name": "B1", "state": "free", "tracks": [[0,0],[1,0]], ... }
    },
    "signals": { ... },
    "stations": { ... },
    "switches": { ... },
    "trains": { ... },
    "speed_profiles": { ... },
    "schedules": { ... },
    "train_routes": { ... }
  }
}
```

**Loading always uses the snapshot.** It provides fast, reliable reconstruction of the full network state.

### Construction Steps (Recovery Fallback)

The `steps` array records the sequence of API-like operations that built the network:

```json
{
  "steps": [
    { "action": "add_track", "params": { "x": 0, "y": 0, "track_bits": 3 } },
    { "action": "add_track", "params": { "x": 1, "y": 0, "track_bits": 3 } },
    { "action": "create_block", "params": { "block_id": "BLK_abc", "tracks": [[0,0],[1,0]] } },
    { "action": "create_signal", "params": { ... } }
  ]
}
```

Steps serve as a **fallback recovery mechanism** if the snapshot becomes corrupted. The `StepPlayer` (`step_player.py`) can replay these steps to reconstruct the network from scratch.

**Known gap:** Steps do not capture all entity types. The following are missing from step recording:
- Station groups / platform assignments
- Train schedules
- Train routes
- Speed profiles
- Some block/signal configuration changes

This means step-based recovery may produce an incomplete network compared to the snapshot.

## Save Mechanics

### Manual Save

Triggered via `POST /api/layouts` with a layout name:

1. `PersistenceManager.save_layout(network, name, description)` is called
2. Network is serialized via `network.to_dict()` (snapshot)
3. Construction steps are collected from `StepRecorder`
4. Both are merged into a single JSON object with metadata
5. File is written atomically (see below)
6. Saved to `layouts/{name}.json`

### Auto-Save

A background daemon thread in `runtime.py` runs auto-save every 5 minutes:

1. Thread sleeps for 300 seconds
2. Calls `PersistenceManager.save_layout(network, "_autosave")`
3. Saves to `layouts/_autosave.json`
4. Thread is a daemon -- dies when the main process exits
5. Errors are caught and logged, never crash the server

### Atomic Writes

All saves use an atomic write pattern to prevent corruption:

```python
# Write to temporary file first
tmp_path = filepath + ".tmp"
with open(tmp_path, 'w') as f:
    json.dump(data, f)  # compact, no indentation

# Atomic rename (on POSIX, this is guaranteed atomic)
os.replace(tmp_path, filepath)
```

This ensures that if the process crashes mid-write, the original file remains intact.

### Compact Format

Files are saved without JSON indentation to minimize size. For large layouts with hundreds of tracks, this significantly reduces file size.

## Load Mechanics

### Manual Load

Triggered via `POST /api/layouts/{name}/load`:

1. `PersistenceManager.load_layout(name)` reads the JSON file
2. The `snapshot` key is extracted
3. `RailwayNetwork.from_dict(snapshot)` reconstructs all entities
4. The loaded network replaces the current one in `AppState`
5. Frontend is notified to refresh

### No Auto-Load on Startup

The system does **not** auto-load a layout on startup. On each fresh start:
- `bootstrap.py` initializes an empty network (or demo data depending on config)
- The user must manually load a layout via the UI
- There is a TODO in `bootstrap.py` to add auto-load of `_autosave.json`

## Schema Migration

Schema migration is minimal. The only migration currently implemented:

- **stations → platforms rename**: Old layouts stored platform data under `stations` key; current code expects `platforms` within station objects. The loader handles both formats.

There is no version field in the file format. If the schema evolves significantly, a versioned migration system would be needed.

## Storage Location

```
layouts/
├── _autosave.json      # Auto-save (overwritten every 5 min)
├── my_layout.json      # User-saved layouts
├── complex_yard.json
└── ...
```

- The `layouts/` directory is created automatically if it doesn't exist
- Layout names are sanitized to prevent path traversal
- List layouts: `GET /api/layouts` returns all `.json` files in the directory
- Delete layout: `DELETE /api/layouts/{name}` removes the file

## Related API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/layouts` | GET | List all saved layouts |
| `/api/layouts` | POST | Save current network as a named layout |
| `/api/layouts/{name}/load` | POST | Load a layout by name |
| `/api/layouts/{name}` | DELETE | Delete a layout |
| `/api/network/clear` | POST | Clear network (start fresh) |
| `/api/network/stats` | GET | Network statistics |

## Design Decisions and Tradeoffs

1. **No database:** Intentional -- this is a local development/hobby tool, not a multi-user system. JSON files are human-readable and easy to back up.

2. **Merged snapshot + steps:** The snapshot provides fast, reliable loading. Steps provide an audit trail and potential recovery path. Having both in one file keeps things simple.

3. **No delta saves:** Every save writes the entire network state. For the typical layout size (hundreds of tracks), this is fast enough. Incremental saves would add complexity without meaningful benefit.

4. **No WebSocket push for save status:** The frontend doesn't know when auto-save happens. It just trusts the backend to do it. A notification could be added but isn't critical.

## Key Source Files

| File | Purpose |
|------|---------|
| `persistence.py` | PersistenceManager - save/load orchestration |
| `step_recorder.py` | StepRecorder - records construction operations |
| `step_player.py` | StepPlayer - replays steps to reconstruct network |
| `bootstrap.py` | Startup initialization (no auto-load yet) |
| `runtime.py` | Background thread management (auto-save timer) |
| `routes/misc.py` | Layout API endpoints |
| `network.py` | `to_dict()` / `from_dict()` serialization |
