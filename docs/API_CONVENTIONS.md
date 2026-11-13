# API Design Conventions

## Overview

The backend exposes a REST API via Flask. Routes are organized in the `routes/` directory, with each module handling a specific domain (tracks, segments, blocks, switches, etc.). This document describes the current conventions, known inconsistencies, and guidelines for new endpoints.

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
from routes import (
    static_files, simulation, layouts, tracks, segments, blocks,
    running_trains, switches, tunnels, settings, hardware,
)

def register_all(app, state):
    static_files.register(app, state)
    simulation.register(app, state)
    layouts.register(app, state)
    # ... etc
```

**Why not Blueprints:** The closure pattern was simpler for a single-developer project. It avoids Blueprint boilerplate and provides direct access to `AppState` without `g` or `current_app` indirection. The tradeoff is that routes aren't self-registering and must be explicitly wired.

## Route Modules

| Module | File | Endpoints |
|--------|------|-----------|
| `static_files` | `routes/static_files.py` | `/` and static files |
| `simulation` | `routes/simulation.py` | `/api/network`, `/api/network/stream`, `/api/network/clear`, `/api/network/stats`, `/api/event-log`, `/api/health`, `/api/server/*` |
| `layouts` | `routes/layouts.py` | `/api/layouts` save, load, list, delete |
| `tracks` | `routes/tracks.py` | `/api/track/{x}/{y}`, `/api/tracks/bulk`, `/api/tracks/bulk-delete` |
| `segments` | `routes/segments.py` | `/api/segments` |
| `blocks` | `routes/blocks.py` | `/api/blocks` |
| `switches` | `routes/switches.py` | `/api/switches`, `/api/tracks/{x}/{y}/switch` |
| `tunnels` | `routes/tunnels.py` | `/api/tunnels` |
| `running_trains` | `routes/running_trains.py` | `/api/trains/*` |
| `settings` | `routes/settings.py` | `/api/settings` |
| `hardware` | `routes/hardware.py` | `/api/hardware/*`, `/api/operation/*`, `/api/virtual-hardware/*`, `/api/debug/hw*` |

The requests themselves are listed in `docs/API.md`.

## Response Format

### Current State (Inconsistent)

The API has two competing success response patterns:

**Pattern A** -- status string:
```json
{ "status": "success", "data": { ... } }
```

**Pattern B** -- boolean flag:
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
| **Plural nouns** | `/api/blocks`, `/api/segments`, `/api/switches` | most |
| **Singular nouns** | `/api/track/{x}/{y}` | 1 |
| **Hyphenated actions** | `/api/segments/{id}/reverse-orientation`, `/api/trains/stop-all` | several |
| **Underscored actions** | `/api/segments/{id}/end_tiles`, `/api/segments/{id}/can_extend` | 2 |

### Guidelines for New Endpoints

1. **Use plural nouns** for collections: `/api/blocks`, `/api/segments`, `/api/trains`
2. **Use `{id}` sub-paths** for individual resources: `/api/blocks/{id}`
3. **Use hyphenated verbs** for actions: `/api/segments/{id}/reverse-orientation`
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
{ "tracks": [ ... ], "blocks": { ... }, "running_trains": { ... } }
```

### Guideline

For new collection endpoints, **bare arrays are acceptable** for simple listings. Use wrapped objects when the response includes multiple entity types or metadata.

## HTTP Methods

| Method | Use |
|--------|-----|
| `GET` | Read data (no side effects) |
| `POST` | Create new resources or trigger actions |
| `PUT` | Update existing resources (full or partial) |
| `PATCH` | Partial update (tiles, segments, blocks; on switches the same as `PUT`) |
| `DELETE` | Remove resources |

Note: both are in use. `PATCH` is used for `/api/track/{x}/{y}`, `/api/segments/{id}`, `/api/blocks/{id}` and `/api/switches/{id}`; `PUT` for switches, settings, hardware and train knob requests.

## Simulation Lock

A request that reads or changes runtime state holds `state.simulation_lock`, so it runs between two runtime steps (design_v2.md **Operator requests**). The lock is taken either in the route or in the service it calls:

| Module | Holds the lock? |
|--------|-----------------|
| `running_trains.py`, `hardware.py`, `simulation.py`, `settings.py` | Yes, in the route |
| `blocks.py`, `segments.py`, `layouts.py` | Yes, in `services/block_service.py`, `segment_service.py`, `layout_service.py` (and in the route for reads that need it) |
| `switches.py` | Yes, in the route for `PUT`, `/state` and `/alternate` |
| `tracks.py`, `tunnels.py` | No |

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
| `routes/*.py` | One module per domain (see Route Modules above) |
| `services/*.py` | The work behind the layout-editing routes |
| `app_factory.py` | Flask app creation + route registration call |
| `app_state.py` | AppState dataclass (dependency injection container) |
