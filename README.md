# Railway Management System

A comprehensive railway management and simulation system implementing train dispatching, scheduling, safety systems, and pathfinding with a 6-bit track geometry system.

## Quick Start

### Running the Demo

The demo includes automated regression testing to ensure no degradation:

```bash
# Run the demo with integrated tests
python3 demo_main.py
```

This will:
1. Run Python backend tests (if pytest is installed)
2. Display test results
3. Start the demo if tests pass
4. Abort if critical tests fail

### Starting the Server

The server also includes automated regression testing:

```bash
# Start server with integrated tests (runs on port 5001)
python3 server.py
```

This will:
1. Run block validation tests (always)
2. Run full backend test suite (if pytest is installed)
3. Initialize demo network if tests pass
4. Start Flask server on port 5001
5. Abort if any tests fail

**Note:** The server will NOT start if tests fail, ensuring you never deploy broken code!

### Testing

**Automated Test Suite (Recommended):**
```bash
# Run all tests with one command
./run_all_tests.sh
# or
python3 run_all_tests.py
```

**Python Backend Tests:**
```bash
# Install pytest first
pip install pytest

# Run backend tests
pytest tests/test_backend.py -v
```

**JavaScript API Tests:**
```bash
# Start the server first
python3 server.py

# In another terminal, run API tests
node tests/test_api.js
```

**Integrated Testing:**
The `demo_main.py` automatically runs Python tests before starting. This ensures:
- No regression in core functionality
- Block validation works correctly
- Signal logic is intact
- Track geometry is valid

**Test Coverage:**
- ✅ Track geometry and validation (42 valid track_bits configurations)
- ✅ Block state management and transitions
- ✅ Signal state calculation and placement validation
- ✅ Station orientation detection (NS, EW, NW_SE, NE_SW)
- ✅ Pathfinding algorithms
- ✅ Dispatcher routing
- ✅ Frontend-backend parity
- ✅ 50+ REST API endpoints
- ✅ JavaScript syntax validation
- ✅ Application smoke tests

## Project Structure

```
atcs6/
├── enums.py                 # Base enumeration types
├── track.py                 # Track segment with 6-bit geometry
├── blocking.py              # Block section safety system
├── block.py                 # Block entity class
├── block_manager.py         # Centralized block state manager
├── block_validation.py      # Block continuity validation
├── interlocking.py          # Interlocking and route management
├── train.py                 # Train and schedule definitions
├── train_config.py          # Train physics configuration
├── train_movement.py        # Train movement calculations
├── train_atc.py             # Automatic Train Control (speed limiting)
├── train_route.py           # Route and waypoint definitions
├── network.py               # Railway network management
├── pathfinder.py            # A* pathfinding for trains
├── dispatcher.py            # Train dispatching system
├── dispatcher_interface.py  # Route dispatcher interface
├── path_dispatcher.py       # Block/switch reservation for paths
├── route_executor.py        # Route and waypoint execution
├── railway_signal.py        # Signal entity class
├── signal_validation.py     # Signal placement validation
├── switch.py                # Switch/turnout entity
├── station.py               # Station entity with platforms
├── speed_profile.py         # Speed-to-voltage mapping
├── block_power.py           # DC voltage output to blocks
├── simulation.py            # Background simulation loop
├── server.py                # Flask REST API server
├── demo_main.py             # Main demonstration script
├── docs/                    # API and architecture documentation
├── static/                  # Web frontend (HTML, CSS, JS)
└── tests/                   # Test suites
```

## Module Descriptions

### enums.py
**Definition:** Core enumeration types for the railway system.

**Classes:**
- `TrackType`: Types of track segments (straight, curve, switch, signal, station)
- `Direction`: Cardinal directions (NORTH, EAST, SOUTH, WEST)
- `SignalState`: Railway signal states (GREEN, RED, YELLOW, DOUBLE_YELLOW)
- `SwitchPosition`: Switch/turnout positions (MAIN, DIVERGING, LOCKED)
- `BlockState`: Block section states (FREE, OCCUPIED, RESERVED, CAUTION)
- `TrainState`: Train operational state for DC analog control (off, idle, standby, running_moving, running_waiting, running_at_station, manual)

**TODO:**
- Add validation methods for enum combinations
- Add enum conversion utilities
- Add documentation examples for each enum usage

---

### track.py
**Definition:** Track segment representation with position, type, and connection rules.

**Classes:**
- `TrackSegment`: Individual railway track section with connections and properties

**Key Features:**
- Track connections mapping (entry_direction -> exit_directions)
- Occupancy and reservation tracking
- Support for straight tracks, curves, switches, signals, and stations

**TODO:**
- Add track segment validation methods
- Implement track wear and maintenance tracking
- Add visual representation methods
- Implement serialization for save/load

---

### blocking.py
**Definition:** Block section system for railway safety - prevents multiple trains in same section.

**Classes:**
- `BlockSection`: Safety block that only allows one train at a time

**Key Features:**
- Reservation system for upcoming trains
- Occupancy tracking
- Entry/exit signal integration

**TODO:**
- Add automatic block detection and creation
- Implement block timing and performance metrics
- Add support for different block types (automatic, manual)
- Implement conflict resolution

---

### interlocking.py
**Definition:** Railway interlocking system ensuring safe route setting.

**Classes:**
- `InterlockingRoute`: Route definition with required switch positions and conflicts
- `InterlockingSystem`: Central interlocking management

**Key Features:**
- Route locking and conflict detection
- Switch position management
- Signal aspect control

**TODO:**
- Add automatic route conflict detection
- Implement approach locking
- Add complex junction support
- Implement automatic route release timing

---

### train.py
**Definition:** Train entities and scheduling system.

**Classes:**
- `Train`: Railway train with physical properties and operational state
- `TrainSchedule`: Complete schedule with stops and timing
- `ScheduleStop`: Individual station stop in schedule

**Key Features:**
- Physical constraints (length, curve radius, speed)
- Schedule management and delay tracking
- Position and movement tracking

**TODO:**
- Add performance metrics and monitoring
- Implement dynamic schedule adjustment
- Add train consist management (multiple cars)
- Implement energy consumption modeling

---

### network.py
**Definition:** Central railway network management coordinating all components.

**Classes:**
- `RailwayNetwork`: Complete railway infrastructure manager

**Key Features:**
- Track network construction and management
- Train, schedule, and block section coordination
- Interlocking system integration
- Network visualization

**TODO:**
- Add network validation and integrity checking
- Implement serialization for save/load
- Add visualization export (SVG, JSON)
- Implement network statistics and analytics

---

### pathfinder.py
**Definition:** A* pathfinding algorithm for trains on railway networks.

**Classes:**
- `RailPathNode`: Node in pathfinding graph
- `RailwayPathfinder`: A* pathfinder implementation

**Key Features:**
- Considers track connections and topology
- Respects signal states and switch positions
- Validates physical constraints (curve radius)
- Calculates optimal paths with cost functions

**TODO:**
- Implement alternative algorithms (Dijkstra, etc.)
- Add dynamic re-routing for blocked paths
- Implement path caching
- Add speed-optimized vs distance-optimized paths

---

### dispatcher.py
**Definition:** Train dispatching and coordination system.

**Classes:**
- `TrainDispatcher`: Central dispatching system

**Key Features:**
- Train route planning and execution
- Block section reservation management
- Schedule conflict detection
- Status reporting and monitoring

**TODO:**
- Implement priority-based scheduling
- Add automatic conflict resolution
- Implement predictive delay analysis
- Add emergency stop and diversion support

---

### demo_main.py
**Definition:** Main demonstration script showing system capabilities.

**Functions:**
- `create_comprehensive_demo()`: Creates demo railway network
- `run_comprehensive_demo()`: Runs complete demonstration

**Demo Features:**
- Main line with stations and signals
- Branch line with switch
- Multiple trains with schedules
- Route locking and conflict detection
- Train movement simulation

---

## Usage

### Run the Web Server

```bash
cd atcs6
python server.py
```

Then open `http://localhost:5001` in your browser.

### Run the Demo Script

```bash
cd atcs6
python demo_main.py
```

### Save and Load Layouts

The system includes a comprehensive layout persistence feature:

**Auto-Save:**
- The system automatically saves your work every 5 minutes to `layouts/_autosave.json`
- This ensures you won't lose progress even if the application crashes

**Manual Save/Load (via Web UI):**
- **Save**: Click the Save button to save the current network layout with a custom name
- **Load**: Click the Load Button to browse and load any saved layout
- **Delete**: Remove unwanted layouts from the list

**File Format:**
- Layouts are saved as compact JSON files in the `layouts/` directory
- Each file contains both a **snapshot** (for fast loading) and **construction steps** (for recovery) in a single merged file
- Snapshot is always used for loading (fast, reliable)
- Steps serve as a fallback if the snapshot becomes corrupted
- Compact format reduces file size (no indentation)

**API Endpoints:**
```bash
# List all layouts
GET /api/layouts

# Save current network as a layout
POST /api/layouts
Body: {"name": "My Layout", "description": "Optional description"}

# Load a layout
POST /api/layouts/{name}/load

# Delete a layout
DELETE /api/layouts/{name}

# Clear the network (start fresh)
POST /api/network/clear

# Get network statistics
GET /api/network/stats
```

See [`docs/API.md`](docs/API.md) for complete API documentation.

## Key Concepts

### Track Connections
The connections dictionary maps `entry_direction -> [exit_directions]` where:
- `entry_direction`: Direction from which train enters the track (opposite of movement)
- `exit_directions`: Possible directions train can exit

### Train Movement
When a train is "heading EAST", it:
- Enters tracks from the WEST side (entry_direction = WEST)
- Exits tracks toward the EAST (exit_direction = EAST)

### Safety Systems
1. **Block Sections**: Prevent multiple trains in same track section
2. **Interlocking**: Ensure safe route setting and prevent conflicting movements
3. **Signals**: Control train entry into block sections
4. **Switches**: Direct trains between different routes

## Future Enhancements

See TODO lists in each module for specific improvements. Key areas:
- Real-time simulation with time-based movement
- Multi-train coordination and automatic conflict resolution
- Performance metrics and analytics
- Advanced visualization
- Energy consumption modeling
- Maintenance scheduling