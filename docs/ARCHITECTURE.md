# ATCS6 Architecture Overview

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         WEB FRONTEND                             │
│  (index.html, CSS, JS: app.js, editor.js, renderer.js, api.js)  │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP REST API
┌────────────────────────────▼────────────────────────────────────┐
│                    FLASK SERVER (server.py)                      │
│  - Network Management: /api/network                              │
│  - Track CRUD: /api/track/{x}/{y}                                │
│  - Bulk Operations: /api/tracks/bulk                             │
│  - Trains: /api/trains                                           │
│  - Simulation: /api/simulation/control                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
┌──────▼──────┐    ┌────────▼────────┐    ┌──────▼──────┐
│RailwayNetwork│    │SimulationController│   │ demo_main  │
│ (network.py) │    │ (simulation.py)    │   │(init data) │
└──────┬───────┘    └────────┬───────────┘   └────────────┘
       │                     │
┌──────┴─────────────────────┴──────┐
│                                   │
▼                                   ▼
┌────────────┐              ┌──────────────┐
│TrackSegment│              │    Train     │
│ (track.py) │              │ (train.py)   │
└────────────┘              └──────────────┘
       │                           │
       ▼                           ▼
┌────────────┐              ┌──────────────┐
│ blocking.py│              │dispatcher.py │
│(BlockSection)│            │(TrainDispatcher)│
└────────────┘              └──────┬───────┘
                                   │
                            ┌──────▼───────┐
                            │pathfinder.py │
                            │(A* routing)  │
                            └──────────────┘
```

## Module Descriptions

| Module | Purpose |
|--------|---------|
| `server.py` | Flask REST API server, handles all HTTP endpoints |
| `network.py` | Central coordinator - manages tracks, trains, blocks |
| `track.py` | TrackSegment class with 6-bit track geometry |
| `train.py` | Train entity with position, speed, schedule |
| `simulation.py` | Background thread running simulation loop |
| `dispatcher.py` | Train routing and scheduling |
| `pathfinder.py` | A* pathfinding for train movement |
| `blocking.py` | Block section safety system |
| `interlocking.py` | Route locking and conflict prevention |
| `enums.py` | Shared enumerations (Direction, SignalState, etc.) |

## Frontend Architecture

| File | Purpose |
|------|---------|
| `static/js/app.js` | Application initialization |
| `static/js/editor.js` | Track editing tools, construction pathfinder |
| `static/js/renderer.js` | Isometric canvas rendering |
| `static/js/api.js` | Server communication wrapper |

## Key Concepts

### 6-Bit Track System

Tracks use a 6-bit encoding for geometry:

| Bit | Value | Path |
|-----|-------|------|
| 0 | 1 | North-South |
| 1 | 2 | East-West |
| 2 | 4 | North-East diagonal |
| 3 | 8 | North-West diagonal |
| 4 | 16 | South-East diagonal |
| 5 | 32 | South-West diagonal |

See `Reference_6Bit_Track_System.md` for complete documentation (1,000+ lines of detailed track geometry docs).

### Two Pathfinders

The system has two separate pathfinding implementations:

1. **Construction Pathfinder** (JavaScript, `editor.js`)
   - Plans track-laying paths for the editor
   - Prefers zigzag patterns for realistic railway construction
   - Avoids creating unnecessary switches
   - Prioritizes using existing tracks

2. **Train Pathfinder** (Python, `pathfinder.py`)
   - Routes trains through existing track network
   - Considers signal states, switch positions, track occupancy
   - A* algorithm with railway-specific constraints

### Coordinate System

- X-axis: West (-) to East (+)
- Y-axis: South (-) to North (+)
- Supports negative coordinates (e.g., `/api/track/-5/10`)

### Dead-End Detection

Dead-ends are automatically computed during propagation:
- A track edge is a dead-end if it has a path but no connecting neighbor
- Changes propagate to adjacent tracks when tracks are added/removed

### Switch System

- Switches are automatically detected when a tile has 2+ track segments
- Switch state: 0 = MAIN (long paths), 1 = DIVERGE (short/diagonal paths)
- All switches on a tile share the same state
