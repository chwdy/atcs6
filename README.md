# Railway Management System

A comprehensive railway management and simulation system implementing train dispatching, scheduling, safety systems, and pathfinding.

## Project Structure

```
atcs6/
├── enums.py           # Base enumeration types
├── track.py           # Track segment definitions
├── blocking.py        # Block section safety system
├── interlocking.py    # Interlocking and route management
├── train.py           # Train and schedule definitions
├── network.py         # Railway network management
├── pathfinder.py      # A* pathfinding for trains
├── dispatcher.py      # Train dispatching system
├── demo_main.py       # Main demonstration script
└── DEMO.py            # Original monolithic demo (deprecated)
```

## Module Descriptions

### enums.py
**Definition:** Core enumeration types for the railway system.

**Classes:**
- `TrackType`: Types of track segments (straight, curve, switch, signal, station)
- `Direction`: Cardinal directions (NORTH, EAST, SOUTH, WEST)
- `SignalState`: Railway signal states (GREEN, RED, YELLOW, DOUBLE_YELLOW)
- `SwitchPosition`: Switch/turnout positions (MAIN, DIVERGING, LOCKED)
- `BlockState`: Block section states (FREE, OCCUPIED, RESERVED, LOCKED)
- `TrainStatus`: Train operational status (IDLE, RUNNING, WAITING, AT_STATION, DELAYED)

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

Run the demonstration:

```bash
cd atcs6
python demo_main.py
```

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
- Network persistence (save/load)
- Advanced visualization
- Energy consumption modeling
- Maintenance scheduling