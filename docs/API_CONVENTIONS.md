# API Design Conventions

## Overview

The backend exposes a REST API via Flask. Routes are organized in the `routes/` directory, with each module handling a specific domain (tracks, blocks, signals, etc.). This document describes the current conventions, known inconsistencies, and guidelines for new endpoints.

## Route Registration Pattern

Routes do **not** use Flask Blueprints. Instead, each route module exposes a `register(app, state)` function that defines endpoints as closures over the shared `AppState`:

```python
# routes/blocks.py
def register(app, state):
    @app.route('/api/blocks', methods=['GET'])
    def get_blocks():
        network = state.network  # Access shared state via closure
        return jsonify([b.to_dict() for b in network.blocks.values()])
```

Registration is centralized in `routes/__init__.py`:

```python
from routes import tracks, blocks, signals, stations, trains, paths, misc, platforms

def register_all(app, state):
    tracks.register(app, state)
    blocks.register(app, state)
    signals.register(app, state)
    # ... etc
```

**Why not Blueprints:** The closure pattern was simpler for a single-developer project. It avoids Blueprint boilerplate and provides direct access to `AppState` without `g` or `current_app` indirection. The tradeoff is that routes aren't self-registering and must be explicitly wired.

## Route Modules

| Module | File | Endpoints | Domain |
|--------|------|-----------|--------|
| `tracks` | `routes/tracks.py` | Track CRUD, bulk operations | Track management |
| `blocks` | `routes/blocks.py` | Block CRUD, track assignment | Block management |
| `signals` | `routes/signals.py` | Signal CRUD, validation | Signal management |
| `stations` | `routes/stations.py` | Station CRUD, orientation | Station management |
| `platforms` | `routes/platforms.py` | Platform management | Platform management |
| `trains` | `routes/trains.py` | Train CRUD, consist management | Train management |
| `paths` | `routes/paths.py` | Pathfinding, dispatcher | Path/dispatch operations |
| `misc` | `routes/misc.py` | Switches, speed profiles, layouts, routes, simulation, static files | Everything else |

**Note:** `misc.py` (~1300 lines) is a catch-all that handles switches, speed profiles, layouts, train routes, simulation control, and static file serving. It should be decomposed into focused modules (e.g., `routes/switches.py`, `routes/simulation.py`, `routes/layouts.py`).

## Response Format

### Current State (Inconsistent)

The API has two competing success response patterns:

**Pattern A** (~53 uses) -- status string:
```json
{ "status": "success", "data": { ... } }
```

**Pattern B** (~11 uses) -- boolean flag:
```json
{ "success": true, "data": { ... } }
```

**Error responses** are more consistent:
```json
{ "error": "Description of what went wrong" }
```
with HTTP status 400 (client error) or 500 (server error).

### Guidelines for New Endpoints

Use **Pattern A** (`"status": "success"`) for consistency with the majority of existing code:

```python
# Success
return jsonify({"status": "success", "block": block.to_dict()}), 200

# Error
return jsonify({"error": "Block not found"}), 404
```

## URL Naming Conventions

### Current State (Inconsistent)

| Convention | Examples | Count |
|-----------|----------|-------|
| **Plural nouns** | `/api/blocks`, `/api/trains`, `/api/switches` | ~12 |
| **Singular nouns** | `/api/signal/{id}`, `/api/station/{id}`, `/api/track/{x}/{y}` | ~8 |
| **Hyphenated actions** | `/api/trains/{id}/set-speed`, `/api/train-routes` | ~6 |
| **Underscored actions** | `/api/blocks/{id}/assign_tracks`, `/api/platforms/{id}/set_block` | ~4 |

### Guidelines for New Endpoints

1. **Use plural nouns** for collections: `/api/blocks`, `/api/signals`, `/api/trains`
2. **Use `{id}` sub-paths** for individual resources: `/api/blocks/{id}`
3. **Use hyphenated verbs** for actions: `/api/trains/{id}/set-speed`
4. **Avoid underscores** in URL paths (use hyphens instead)

```
# Good
GET    /api/blocks              # List all blocks
POST   /api/blocks              # Create a block
GET    /api/blocks/{id}         # Get one block
PUT    /api/blocks/{id}         # Update a block
DELETE /api/blocks/{id}         # Delete a block
POST   /api/blocks/{id}/assign-tracks   # Action on a block

# Avoid
GET    /api/block               # Singular collection name
POST   /api/blocks/{id}/assign_tracks   # Underscored action
```

## Collection Response Shapes

### Current State (Inconsistent)

Some endpoints return bare arrays, others wrap them in an object:

```json
// Bare array (e.g., GET /api/blocks)
[{ "block_id": "BLK_1", ... }, { "block_id": "BLK_2", ... }]

// Wrapped (e.g., GET /api/network)
{ "tracks": { ... }, "blocks": { ... }, "trains": { ... } }
```

### Guideline

For new collection endpoints, **bare arrays are acceptable** for simple listings. Use wrapped objects when the response includes multiple entity types or metadata.

## HTTP Methods

| Method | Use |
|--------|-----|
| `GET` | Read data (no side effects) |
| `POST` | Create new resources or trigger actions |
| `PUT` | Update existing resources (full or partial) |
| `DELETE` | Remove resources |

Note: The API does not use `PATCH` -- all updates use `PUT`.

## Simulation Lock

Some route modules acquire the simulation lock (`state.simulation_lock`) before modifying network state to prevent race conditions with the simulation tick loop. Usage is **inconsistent** across modules:

| Module | Uses Lock? | Notes |
|--------|-----------|-------|
| `tracks.py` | Yes | Track mutations lock |
| `blocks.py` | Partial | Some endpoints lock, some don't |
| `signals.py` | No | Should lock for signal state changes |
| `stations.py` | Partial | |
| `trains.py` | Yes | Train mutations lock |
| `misc.py` | Partial | Simulation control locks, others don't |

**Guideline:** Any endpoint that **mutates** network state should acquire the simulation lock:

```python
@app.route('/api/blocks/<block_id>', methods=['PUT'])
def update_block(block_id):
    with state.simulation_lock:
        # ... modify block ...
        return jsonify({"status": "success"})
```

Read-only endpoints (`GET`) do not need the lock.

## Authentication

There is no authentication. This is intentional -- the application is a local development tool, not a multi-user web service. All endpoints are accessible without credentials.

## Error Handling Pattern

```python
@app.route('/api/blocks/<block_id>', methods=['GET'])
def get_block(block_id):
    try:
        block = state.network.blocks.get(block_id)
        if not block:
            return jsonify({"error": f"Block {block_id} not found"}), 404
        return jsonify(block.to_dict()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

- Use `404` for "resource not found"
- Use `400` for invalid input / validation errors
- Use `500` for unexpected server errors
- Always return JSON with an `"error"` key for error responses

## Key Source Files

| File | Purpose |
|------|---------|
| `routes/__init__.py` | Route registration orchestrator |
| `routes/tracks.py` | Track CRUD |
| `routes/blocks.py` | Block management |
| `routes/signals.py` | Signal management |
| `routes/stations.py` | Station management |
| `routes/platforms.py` | Platform management |
| `routes/trains.py` | Train management |
| `routes/paths.py` | Pathfinding + dispatcher |
| `routes/misc.py` | Switches, layouts, simulation, routes, speed profiles |
| `app_factory.py` | Flask app creation + route registration call |
| `app_state.py` | AppState dataclass (dependency injection container) |
