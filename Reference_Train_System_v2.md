# Technical Specification: Train System v2

> **STATUS: AUTHORITATIVE SPECIFICATION**  
> This is the current, implemented train system architecture. See `archive/Reference_Train_System.md` for the previous version.

> **Document Type:** Technical Implementation Specification  
> **Version:** 2.0  
> **Created:** January 2026  
> **Status:** Design Complete

## Table of Contents

1. [Overview & Core Concepts](#1-overview--core-concepts)
2. [Simplified Architecture](#2-simplified-architecture)
3. [TrainController Specification](#3-traincontroller-specification)
4. [TrainPhysics Specification](#4-trainphysics-specification)
5. [Train Entity Specification](#5-train-entity-specification)
6. [SpeedProfile & Handle Model](#6-speedprofile--handle-model)
7. [Route & Waypoint Specification](#7-route--waypoint-specification)
8. [Block & Power Control](#8-block--power-control)
9. [SimulationController Integration](#9-simulationcontroller-integration)
10. [API Endpoints](#10-api-endpoints)
11. [State Machine Diagrams](#11-state-machine-diagrams)
12. [Example Scenarios](#12-example-scenarios)

---

## 1. Overview & Core Concepts

### 1.1 Purpose

This document specifies the Train System for the railway management platform, enabling:

- Train placement on tracks with multi-block occupancy based on length
- Directional movement (forward/reverse) with automatic turn-around at waypoints
- Route-based autonomous travel (ATO) with waypoints and timing
- Manual driving with throttle/brake handle control
- DC analog block control (voltage → speed mapping)
- Automatic Train Control (ATC) for safe speed regulation

### 1.2 Design Philosophy: Simplicity

The previous design had overlapping components (`Train`, `OnboardComputer`, `TrainMover`, `TrainATC`) with unclear responsibilities. This v2 design consolidates into two clean classes:

| Component | Responsibility |
|-----------|----------------|
| **TrainController** | Decides WHAT speed the train should have (ATC limits, ATO decisions, manual input) |
| **TrainPhysics** | Executes HOW the train moves (position updates, block management, voltage output) |

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SIMPLIFIED MODEL                                │
│                                                                             │
│   Command Sources          Controller              Physics                  │
│   ───────────────          ──────────              ───────                  │
│                                                                             │
│   ┌─────────────┐         ┌─────────────────┐     ┌─────────────────┐       │
│   │ ATO (Route) │─────────►                 │     │                 │       │
│   └─────────────┘         │ TrainController │     │  TrainPhysics   │       │
│                           │                 │────►│                 │       │
│   ┌─────────────┐         │ • ATC limits    │     │ • Move position │       │
│   │ Manual (UI) │─────────►│ • Handle logic │     │ • Update speed  │       │
│   └─────────────┘         │ • State machine │     │ • Block occupy  │       │
│                           │ • Lookahead     │     │ • Voltage output│       │
│                           └─────────────────┘     └─────────────────┘       │
│                                  │                        │                 │
│                                  │                        │                 │
│                                  ▼                        ▼                 │
│                           ┌─────────────┐          ┌─────────────┐          │
│                           │    Train    │          │BlockManager │          │
│                           │   (Data)    │          │PowerController│         │
│                           └─────────────┘          └─────────────┘          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 Control Model: DC Analog Block Control

```
┌─────────────────────────────────────────────────────────────────┐
│                    DC ANALOG CONTROL MODEL                       │
│                                                                  │
│   Train ──► SpeedProfile ──► Voltage ──► BlockPowerController   │
│     │                                            │               │
│     │  "I want 60 km/h"    "Output 8V"          │               │
│     │                                            ▼               │
│     │                                    ┌─────────────┐         │
│     │                                    │   Block A   │ 8V      │
│     │                                    │   Block B   │ 8V      │
│     │                                    │   Block C   │ 0V      │
│     │                                    └─────────────┘         │
│     │                                            │               │
│     └────────── ATC ◄── "Clear distance: 200cm" ─┘               │
│                  │                                               │
│                  └─► Limits target speed based on clearance      │
└─────────────────────────────────────────────────────────────────┘
```

**Key Principle:** The train doesn't have power. The blocks output voltage, and the train's physical motors respond. The software controls block voltage based on what trains occupy them.

### 1.4 Units

| Measurement | Unit | Notes |
|-------------|------|-------|
| Speed | km/h | Scale speed (what the model represents) |
| Distance | cm | Centimeters (physical layout measurement) |
| Voltage | V (0-12) | DC voltage output to track |
| Time | seconds | For acceleration/deceleration rates |
| Acceleration | km/h/s | Speed change per second |

### 1.5 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Controller per train | Yes | Each train has its own TrainController instance |
| Physics per train | Yes | Each train has its own TrainPhysics instance |
| Ownership | SimulationController | Sim owns both controllers and physics instances |
| Handle model | Realistic (P1-P5, N, B1-B8, EB) | Matches real train operation |
| ATC + ATO | Combined in TrainController | Simpler than separate classes |
| State storage | Split: Train (physical), Controller (operational) | Clear separation |
| Block interaction | TrainPhysics direct | Physics directly calls BlockManager |

### 1.6 Simulation Tick Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SIMULATION TICK (every 100ms)                       │
│                          SimulationController.update(delta)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ For each train:
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: TrainController.tick(delta)                                         │
│  ─────────────────────────────────────                                       │
│  • Calculate clearance ahead (ATC)                                           │
│  • Calculate speed limit (braking curve)                                     │
│  • If ATO mode: determine handle_position to follow route/schedule           │
│  • If Manual mode: use user-set handle_position                              │
│  • Store handle_position for physics to read                                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: TrainPhysics.tick(delta)                                            │
│  ─────────────────────────────────                                           │
│  • Read handle_position from controller                                      │
│  • Calculate acceleration from handle (using SpeedProfile rates)             │
│  • Update train.current_speed based on acceleration and delta                │
│  • Move train position (head and tail) based on speed and delta              │
│  • Update block occupancy (occupy new blocks, release vacated blocks)        │
│  • Output voltage to BlockPowerController based on target speed              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Simplified Architecture

### 2.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BACKEND (Python)                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                     SimulationController                              │   │
│  │                     (Orchestrator / Clock)                            │   │
│  │  ─────────────────────────────────────────────────────────────────   │   │
│  │  • Owns simulation_time                                               │   │
│  │  • Owns controllers: Dict[str, TrainController]                       │   │
│  │  • Owns physics: Dict[str, TrainPhysics]                              │   │
│  │  • Calls update(delta) on tick                                        │   │
│  │  • Iterates all trains, calls controller.tick() then physics.tick()  │   │
│  └───────────────────────────────┬──────────────────────────────────────┘   │
│                                  │                                          │
│                  ┌───────────────┴───────────────┐                          │
│                  │                               │                          │
│                  ▼                               ▼                          │
│  ┌──────────────────────────────┐ ┌──────────────────────────────┐          │
│  │      TrainController         │ │       TrainPhysics           │          │
│  │      (One per train)         │ │       (One per train)        │          │
│  │  ──────────────────────────  │ │  ──────────────────────────  │          │
│  │                              │ │                              │          │
│  │  MERGES:                     │ │  RENAMED FROM:               │          │
│  │  • OnboardComputer state     │ │  • TrainMover                │          │
│  │  • TrainATC logic            │ │                              │          │
│  │  • ATO driving logic         │ │  DOES:                       │          │
│  │                              │ │  • Move train position       │          │
│  │  STORES (operational):       │ │  • Update train speed        │          │
│  │  • state (OFF/IDLE/etc)      │ │  • Block occupy/release      │          │
│  │  • target_speed              │ │  • Voltage output            │          │
│  │  • handle_position           │ │                              │          │
│  │  • power_on, emergency_stop  │ │  READS FROM CONTROLLER:      │          │
│  │  • active_cab                │ │  • handle_position           │          │
│  │  • control_mode (ato/manual) │ │  • power_on state            │          │
│  │  • ato_running               │ │  • emergency_stop state      │          │
│  │                              │ │                              │          │
│  │  CALCULATES:                 │ │  WRITES TO TRAIN:            │          │
│  │  • Clearance ahead (ATC)     │ │  • current_speed             │          │
│  │  • Braking curve limits      │ │  • position (head/tail)      │          │
│  │  • ATO handle selection      │ │  • occupied_blocks           │          │
│  │                              │ │                              │          │
│  │  VALIDATES COMMANDS:         │ │  WRITES TO WORLD:            │          │
│  │  • set_cab(), power_on/off   │ │  • BlockManager states       │          │
│  │  • set_handle(), ato_go/stop │ │  • BlockPowerController V    │          │
│  └──────────────────────────────┘ └──────────────────────────────┘          │
│                  │                               │                          │
│                  │  reads/writes                 │  reads/writes            │
│                  ▼                               ▼                          │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                              Train                                    │   │
│  │                         (Data Entity)                                 │   │
│  │  ────────────────────────────────────────────────────────────────    │   │
│  │                                                                       │   │
│  │  STORES (physical properties):                                        │   │
│  │  • position (TrainPosition: head tile, tail tile, distances)          │   │
│  │  • current_speed (km/h - actual speed from physics)                   │   │
│  │  • current_path (list of tiles from pathfinder)                       │   │
│  │  • facing_direction (Direction enum)                                  │   │
│  │  • active_cab ('A', 'B', None)                                        │   │
│  │  • length_cm, max_speed, model                                        │   │
│  │  • speed_profile_id                                                   │   │
│  │                                                                       │   │
│  │  DOES NOT STORE:                                                      │   │
│  │  • Operational state (that's in TrainController)                      │   │
│  │  • target_speed (that's in TrainController)                           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌────────────────────────────────┐  ┌────────────────────────────────┐     │
│  │        BlockManager            │  │     BlockPowerController       │     │
│  │  ────────────────────────────  │  │  ────────────────────────────  │     │
│  │  • occupy_block(block_id)      │  │  • set_block_voltage()         │     │
│  │  • release_block(block_id)     │  │  • update_for_train()          │     │
│  │  • clear_block(block_id)       │  │  • emergency_stop_all()        │     │
│  │  • State: FREE→RESERVED→       │  │                                │     │
│  │    OCCUPIED→CAUTION→FREE       │  │  Uses SpeedProfile to convert  │     │
│  └────────────────────────────────┘  │  target_speed → voltage        │     │
│                                      └────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Entity Relationships

```
                                    ┌─────────────────────┐
                                    │   SpeedProfile      │
                                    │   ─────────────     │
                                    │   profile_id        │
                                    │   speed_voltage_map │
                                    │   accel_rate        │
                                    │   decel_rate        │
                                    │   emergency_rate    │
                                    └──────────┬──────────┘
                                               │
                                               │ has_profile
                                               ▼
┌──────────────────┐                ┌─────────────────────┐
│ TrainController  │                │       Train         │
│  ──────────────  │   controls     │   ───────────       │
│  state           │◄──────────────►│   train_id          │
│  target_speed    │                │   position          │
│  handle_position │                │   current_speed     │
│  ato_running     │                │   facing_direction  │
│  power_on        │                │   current_path      │
│  active_cab      │                │   active_cab        │
└────────┬─────────┘                └──────────┬──────────┘
         │                                     │
         │ ATC queries                         │ occupies (based on length)
         ▼                                     ▼
┌──────────────────┐                ┌─────────────────────┐
│  TrainPhysics    │                │      Block          │
│  ──────────────  │                │   ───────────       │
│  move_train()    │────────────────►   block_id          │
│  update_speed()  │   occupies/    │   current_voltage   │
│  update_blocks() │   releases     │   occupied_by       │
│  output_voltage()│                │   length_cm         │
└──────────────────┘                └─────────────────────┘
         │                                     ▲
         │ controls voltage                    │
         ▼                                     │ monitors
┌──────────────────────────┐                   │
│  BlockPowerController    │                   │
│  ──────────────────────  │        ┌──────────┴──────────┐
│  set_block_voltage()     │        │    BlockManager     │
│  update_for_train()      │        │  ─────────────────  │
│  emergency_stop_all()    │        │  occupy_block()     │
└──────────────────────────┘        │  release_block()    │
                                    │  clear_block()      │
                                    │  state machine      │
                                    └─────────────────────┘
```

### 2.3 Current Files

| File | Purpose |
|------|---------|
| `train_controller.py` | Unified ATC + ATO + command handling |
| `train_physics.py` | Physical movement, block occupancy, voltage output |
| `train.py` | Train data entity with TrainPosition |
| `simulation.py` | SimulationController using TrainController + TrainPhysics |
| `server.py` | REST API endpoints for train control |

### 2.4 What Was Removed (Consolidation)

The following classes/files are consolidated into TrainController:

| Old Component | What It Did | Where It Goes Now |
|---------------|-------------|-------------------|
| `OnboardComputer` | Power, cab, mode, handle state | TrainController state |
| `TrainATC` | Clearance calculation, speed limits | TrainController.get_atc_limit() |
| `Train.stop()` | Set speed to 0 | TrainController.ato_stop() |
| `Train.depart()` | Start movement | TrainController.ato_go() |
| `Train.turn_around()` | Flip direction | TrainController.set_cab() |

---

## 3. TrainController Specification

### 3.1 Purpose

TrainController is the **onboard computer** for each train. It combines:
- **ATC (Automatic Train Control):** Safety system that calculates speed limits based on clearance
- **ATO (Automatic Train Operation):** Automatic driving that follows routes/schedules
- **Command Handling:** Validates and executes user commands (power, cab, mode, handle)

Think of it like the cab computer in a real train that:
- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Knows which cab is active and the travel direction
- Manages the throttle/brake handle position
- Decides whether buttons/commands should work
- Calculates safe speed limits based on clearance ahead
- In ATO mode, automatically drives toward destination

### 3.2 Design Goals

| Goal | Description |
|------|-------------|
| **Single Authority** | TrainController owns all operational state (cab, mode, ATO, power) |
| **Combined ATC+ATO** | One class handles both safety limits AND automatic driving |
| **Command Validation** | All commands go through TrainController which validates preconditions |
| **Read-Only UI** | Control panel reads from TrainController state, never computes locally |
| **Separation from Physics** | TrainController decides WHAT speed; TrainPhysics executes movement |

### 3.3 Properties

#### 3.3.1 Power & Safety State

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `power_on` | `bool` | `False` | Whether train power is on |
| `emergency_stop_active` | `bool` | `False` | Emergency stop engaged |

#### 3.3.2 Cab & Mode State

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `active_cab` | `Optional[str]` | `None` | Which cab is active: `'A'`, `'B'`, or `None` |
| `control_mode` | `str` | `'ato'` | Current mode: `'ato'` or `'manual'` |
| `travel_direction` | `str` | `'forward'` | Direction relative to cab: `'forward'` or `'reverse'` |

#### 3.3.3 Operational State

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `state` | `TrainState` | `OFF` | Current operational state (see §3.4) |
| `target_speed` | `float` | `0.0` | Desired speed in km/h (ATC-limited) |
| `handle_position` | `HandlePosition` | `N` | Current throttle/brake handle position |

#### 3.3.4 ATO State

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `ato_running` | `bool` | `False` | Whether ATO is actively running |
| `ato_destination_id` | `Optional[str]` | `None` | Target station/block for ATO |
| `ato_path_id` | `Optional[str]` | `None` | Assigned path ID from dispatcher |

#### 3.3.5 ATC State (calculated each tick)

| Property | Type | Description |
|----------|------|-------------|
| `clearance_ahead_cm` | `float` | Distance to next obstruction |
| `atc_speed_limit` | `float` | Maximum safe speed based on clearance |
| `current_block_speed_limit` | `float` | Speed limit of current block |

### 3.4 TrainState Enum

The `TrainState` enum is the primary operational state:

| State | Voltage | Description |
|-------|---------|-------------|
| **OFF** | 0V | Train shut off, no power |
| **IDLE** | Lighting | Powered, no cab selected, red lights both ends (if safe) |
| **STANDBY** | Lighting | Cab selected, ready to depart, headlight + taillight |
| **RUNNING_MOVING** | Speed-based | Train is moving OR intends to move (path is clear) |
| **RUNNING_WAITING** | Lighting | Stopped due to external obstruction (signal, blocked path) |
| **RUNNING_AT_STATION** | Lighting | Dwelling at station (intentional stop) |
| **MANUAL** | User-controlled | User controls speed via handle, ATC still protects |

**Important Distinction:**
- `RUNNING_MOVING`: Train wants to move and CAN move (path clear, ATC allows)
- `RUNNING_WAITING`: Train wants to move but CANNOT (external obstruction)
- `RUNNING_AT_STATION`: Train is intentionally stopped (scheduled dwell)

`RUNNING_WAITING` is **not** triggered by speed reaching zero. It is triggered by:
- Red signal ahead
- Block occupied by another train
- Path not yet reserved/available
- ATC speed limit = 0 due to obstruction

When the obstruction clears, the train automatically transitions back to `RUNNING_MOVING`.

### 3.5 State Machine

```
                                    ┌─────────────────────────────────────────────────────┐
                                    │                   RUNNING (group)                    │
                                    │                                                     │
                                    │  ┌─────────────┐      ┌─────────────┐               │
                                    │  │   MOVING    │      │ AT_STATION  │               │
                                    │  │ (speed-V)   │      │ (lighting)  │               │
                                    │  └──────┬──────┘      └──────┬──────┘               │
                                    │         │                    │                      │
                                    │         │ obstruction        │ arrive_at_station()  │
                                    │         │ (ATC limit=0)      │                      │
                                    │         ▼                    │                      │
                                    │  ┌─────────────┐             │                      │
                                    │  │   WAITING   │◄────────────┘                      │
                                    │  │ (lighting)  │                                    │
                                    │  └──────┬──────┘                                    │
                                    │         │                                           │
                                    │         │ obstruction clears                        │
                                    │         │ (ATC limit > 0)                           │
                                    │         │ AUTO-TRANSITION                           │
                                    │         ▼                                           │
                                    │  ┌─────────────┐                                    │
                                    │  │   MOVING    │ ◄──── resumes ATO if enabled       │
                                    │  └─────────────┘                                    │
                                    │                                                     │
                                    └─────────────────────────┬───────────────────────────┘
                                                              ▲
                                                 ato_go()     │     arrive() / ato_stop()
                                                              │
┌───────────────────┐                 ┌───────────────────────┴───────────────────────┐
│      MANUAL       │◄───────────────►│                   STANDBY                      │
│  User controls    │    set_mode()   │  Cab selected, lighting voltage                │
│  handle, ATC      │    (stopped     │  Ready to depart                               │
│  still protects   │     only)       └───────────────────────┬───────────────────────┘
└───────────────────┘                                         ▲
                                             set_cab('A'/'B') │  set_cab(None)
                                                              │
                                       ┌──────────────────────┴──────────────────────┐
                                       │                    IDLE                      │
                                       │  Powered, no cab selected, lighting V        │
                                       └──────────────────────┬──────────────────────┘
                                                              ▲
                                                 power_on()   │   power_off()
                                                              │
                                       ┌──────────────────────┴──────────────────────┐
                                       │                     OFF                      │
                                       │  0V, no power                                │
                                       └──────────────────────────────────────────────┘
```

**Key State Transitions:**

| From | To | Trigger | Notes |
|------|-----|---------|-------|
| RUNNING_MOVING | RUNNING_WAITING | ATC limit = 0 AND train stopped | External obstruction detected |
| RUNNING_WAITING | RUNNING_MOVING | ATC limit > 0 | **Auto-transition** when path clears |
| RUNNING_MOVING | RUNNING_AT_STATION | arrive_at_station() | Reached scheduled stop |
| RUNNING_AT_STATION | STANDBY | depart_from_station() | Ready for next waypoint |

**Critical: WAITING ↔ MOVING is Automatic**

The transition between WAITING and MOVING is managed automatically by the controller's `tick()` method based on ATC clearance. This ensures trains resume movement immediately when obstructions clear, without requiring manual intervention.

### 3.6 Command Methods

#### 3.6.1 Power Commands

| Method | Validation | Effect |
|--------|------------|--------|
| `power_on()` | Must be OFF | Sets `power_on=True`, state→IDLE |
| `power_off()` | Must be stopped | Sets `power_on=False`, state→OFF |
| `emergency_stop()` | Always allowed | Sets `emergency_stop_active=True`, handle→EB |
| `release_emergency()` | Stopped, handle in N/B | Clears emergency, handle→N |

#### 3.6.2 Cab & Mode Commands

| Method | Validation | Effect |
|--------|------------|--------|
| `set_cab('A'/'B'/None)` | Must be stopped | Sets `active_cab`, flips `facing_direction` if switching A↔B |
| `set_mode('ato'/'manual')` | Must be stopped | Sets `control_mode`, updates state |
| `set_travel_direction('forward'/'reverse')` | Stopped, manual mode | Sets direction for reverse driving |

#### 3.6.3 Handle Commands

| Method | Validation | Effect |
|--------|------------|--------|
| `set_handle(position)` | Powered on; in manual mode (or ATO not running) | Sets `handle_position` |

#### 3.6.4 ATO Commands

| Method | Validation | Effect |
|--------|------------|--------|
| `ato_go()` | ATO mode, cab set, STANDBY state | Sets `ato_running=True`, state→RUNNING_MOVING |
| `ato_stop()` | ATO running | Sets `ato_running=False`, train decelerates to stop |

### 3.7 Tick Method (Called Each Simulation Tick)

The `tick()` method is called every simulation tick and is responsible for:
1. Calculating ATC safety limits
2. Managing state transitions between RUNNING_MOVING and RUNNING_WAITING
3. Calculating target speed and handle position

**Critical Design Principle:** The RUNNING_WAITING state represents an external obstruction, not simply "speed = 0". The controller must:
- Transition MOVING → WAITING when an obstruction forces a stop
- **Automatically** transition WAITING → MOVING when the obstruction clears

```python
def tick(self, delta: float) -> None:
    """
    Called each simulation tick by SimulationController.
    
    Calculates ATC limits and ATO handle decisions.
    Does NOT move the train - that's TrainPhysics.tick().
    """
    train = self._get_train()
    
    # 1. Skip if powered off
    if not self.power_on:
        self.target_speed = 0.0
        self.state = TrainState.OFF
        return
    
    # 2. Calculate ATC (safety) limits - always do this first
    self.clearance_ahead_cm = self._calculate_clearance_ahead()
    self.atc_speed_limit, self.speed_limit_reason = self._calculate_atc_speed_limit()
    self.current_block_speed_limit = self._get_current_block_speed_limit()
    
    # 3. Emergency stop override
    if self.emergency_stop_active:
        self.handle_position = HandlePosition.EB
        self.target_speed = 0.0
        return
    
    # 4. Calculate effective speed limit (used by multiple states)
    effective_limit = min(
        train.max_speed,
        self.atc_speed_limit,
        self.current_block_speed_limit
    )
    
    # 5. State-specific behavior
    if self.state == TrainState.RUNNING_MOVING:
        # Train is moving or intends to move
        if self.control_mode == 'ato' and self.ato_running:
            self.handle_position = self._ato_calculate_handle()
        
        self.target_speed = effective_limit
        train.speed_limit_reason = self.speed_limit_reason
        
        # Check if obstruction forces a stop
        if effective_limit <= STOPPED_SPEED_THRESHOLD_KMH:
            if train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                # Train has stopped due to obstruction - transition to WAITING
                self.state = TrainState.RUNNING_WAITING
                train.state = TrainState.RUNNING_WAITING
                train.wait_reason = self.speed_limit_reason or "Obstruction ahead"
    
    elif self.state == TrainState.RUNNING_WAITING:
        # Train is stopped due to obstruction - check if path cleared
        if effective_limit > STOPPED_SPEED_THRESHOLD_KMH:
            # Path is clear - resume movement
            self.state = TrainState.RUNNING_MOVING
            train.state = TrainState.RUNNING_MOVING
            train.wait_reason = None
            
            # Restart ATO if in ATO mode
            if self.control_mode == 'ato':
                self.ato_running = True
            
            # Set target speed for resuming
            self.target_speed = effective_limit
            
            # ATO will calculate handle, or manual uses current handle
            if self.control_mode == 'ato' and self.ato_running:
                self.handle_position = self._ato_calculate_handle()
        else:
            # Still obstructed - remain waiting
            self.target_speed = 0.0
            self.handle_position = HandlePosition.N
    
    elif self.state == TrainState.MANUAL:
        # Manual mode - user controls via handle, ATC still limits
        self.target_speed = effective_limit
        train.speed_limit_reason = self.speed_limit_reason
    
    else:
        # IDLE, STANDBY, AT_STATION, OFF: not actively moving
        self.target_speed = 0.0
        if self.state != TrainState.OFF:
            self.handle_position = HandlePosition.N
    
    # 6. Update train's target_speed for reference
    train.target_speed = self.target_speed
```

**State Transition Logic Summary:**

```
┌────────────────────────────────────────────────────────────────────────┐
│                         TICK() STATE LOGIC                              │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  RUNNING_MOVING:                                                       │
│    ├─ Calculate target_speed from ATC/ATO                              │
│    ├─ IF effective_limit ≤ threshold AND speed ≤ threshold:           │
│    │     → Transition to RUNNING_WAITING (obstruction detected)        │
│    └─ ELSE: Continue moving                                            │
│                                                                        │
│  RUNNING_WAITING:                                                      │
│    ├─ Check: Is effective_limit > threshold?                           │
│    ├─ IF YES (path clear):                                             │
│    │     → Transition to RUNNING_MOVING                                │
│    │     → Restart ATO if enabled                                      │
│    │     → Set target_speed = effective_limit                          │
│    └─ IF NO (still blocked):                                           │
│          → target_speed = 0, handle = N                                │
│                                                                        │
│  MANUAL:                                                               │
│    └─ target_speed = effective_limit (ATC protection active)           │
│                                                                        │
│  IDLE / STANDBY / AT_STATION:                                          │
│    └─ target_speed = 0 (intentionally not moving)                      │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.8 ATC (Automatic Train Control) Logic

#### 3.8.1 Clearance Calculation

```python
def _calculate_clearance_ahead(self) -> float:
    """
    Calculate available clearance in cm.
    
    Follows the train's current path forward, summing block lengths
    until hitting an obstruction:
    - Block occupied by another train
    - Block not reserved for this train's path
    - Red signal
    - End of path
    
    Returns:
        Distance in cm until next obstruction
    """
    clearance = 0.0
    train = self._get_train()
    
    # Start from train's current position
    current_block = self._get_current_block()
    if not current_block:
        return 0.0
    
    # Add remaining distance in current block
    clearance += self._get_remaining_distance_in_block()
    
    # Follow path forward
    for block_id in self._get_blocks_ahead():
        block = self.network.blocks.get(block_id)
        if not block:
            break
        
        # Check if block is clear
        if block.state == BlockState.OCCUPIED:
            if block.occupied_by != train.train_id:
                break  # Another train ahead
        
        # Check signals
        if self._is_signal_red_for_block(block_id):
            break
        
        clearance += block.length_cm
    
    return clearance
```

#### 3.8.2 Braking Curve Calculation

```python
def _calculate_atc_speed_limit(self) -> float:
    """
    Calculate maximum safe speed based on clearance ahead.
    
    Uses physics: v² = 2 * a * d
    
    Returns:
        Maximum safe speed in km/h
    """
    clearance = self.clearance_ahead_cm
    profile = self._get_speed_profile()
    
    safety_margin_cm = 50.0  # Buffer before obstruction
    effective_distance = max(0, clearance - safety_margin_cm)
    
    # Convert deceleration from km/h/s to cm/s²
    # 1 km/h = 100000/3600 cm/s ≈ 27.78 cm/s
    decel_cm_s2 = profile.deceleration_rate * (100000 / 3600)
    
    # v² = 2 * a * d
    v_cm_s_squared = 2 * decel_cm_s2 * effective_distance
    v_cm_s = math.sqrt(v_cm_s_squared) if v_cm_s_squared > 0 else 0
    
    # Convert back to km/h
    v_kmh = v_cm_s * 3600 / 100000
    
    # Also check for yellow signals (caution speed)
    if self._is_approaching_yellow_signal():
        v_kmh = min(v_kmh, YELLOW_SIGNAL_SPEED_KMH)
    
    return v_kmh
```

### 3.9 Automatic WAITING ↔ MOVING Transitions

The transition between `RUNNING_WAITING` and `RUNNING_MOVING` is **automatic** and managed by the controller's `tick()` method. This is critical for correct train operation.

#### 3.9.1 When MOVING → WAITING Occurs

A train transitions from `RUNNING_MOVING` to `RUNNING_WAITING` when ALL of these are true:
1. **ATC speed limit ≤ threshold** (obstruction detected)
2. **Train's current speed ≤ threshold** (train has physically stopped)
3. **Reason exists** (signal, block occupied, path unavailable)

```python
# In tick(), within RUNNING_MOVING state handling:
if effective_limit <= STOPPED_SPEED_THRESHOLD_KMH:
    if train.current_speed <= STOPPED_SPEED_THRESHOLD_KMH:
        self.state = TrainState.RUNNING_WAITING
        train.wait_reason = self.speed_limit_reason or "Obstruction ahead"
```

**Important:** Simply reaching speed = 0 does NOT trigger this transition. There must be an external obstruction (low ATC limit).

#### 3.9.2 When WAITING → MOVING Occurs

A train transitions from `RUNNING_WAITING` to `RUNNING_MOVING` when:
1. **ATC speed limit > threshold** (obstruction cleared)

```python
# In tick(), within RUNNING_WAITING state handling:
if effective_limit > STOPPED_SPEED_THRESHOLD_KMH:
    self.state = TrainState.RUNNING_MOVING
    train.wait_reason = None
    
    # Restart ATO if in ATO mode
    if self.control_mode == 'ato':
        self.ato_running = True
    
    self.target_speed = effective_limit
```

#### 3.9.3 Why This Matters

Without automatic WAITING → MOVING transition:
- Train stops due to obstruction (correct)
- Obstruction clears, ATC limit increases (correct)
- Train remains in WAITING state forever (BUG!)
- `tick()` sets target_speed = 0 for WAITING state (BUG!)
- Train never moves again (DEADLOCK!)

With automatic transition:
- Train stops due to obstruction
- Obstruction clears, ATC limit increases
- `tick()` detects ATC limit > 0, transitions to MOVING
- `tick()` sets target_speed = effective_limit
- Train resumes movement automatically

### 3.10 ATO (Automatic Train Operation) Logic

#### 3.10.1 Handle Selection

When in ATO mode, the controller automatically selects the appropriate handle position:

```python
def _ato_calculate_handle(self) -> HandlePosition:
    """
    ATO automatically selects appropriate handle position.
    
    The ATO system:
    1. Respects ATC limits (never exceeds)
    2. Follows scheduled speeds
    3. Brakes smoothly for stops
    """
    train = self._get_train()
    current_speed = train.current_speed
    
    # Get target from route (or max if free running)
    route_target = self._get_route_target_speed()
    effective_target = min(route_target, self.atc_speed_limit)
    
    # Need to stop (at station, end of path, ATC limit = 0)
    if effective_target == 0:
        if current_speed > 0:
            return self._calculate_braking_notch(current_speed)
        return HandlePosition.N
    
    # Distance to next stop (for smooth braking)
    distance_to_stop = self._get_distance_to_next_stop()
    if distance_to_stop < self._get_braking_distance(current_speed):
        return self._calculate_braking_notch(current_speed)
    
    # At target speed - coast
    if abs(current_speed - effective_target) < SPEED_TOLERANCE:
        return HandlePosition.N
    
    # Below target - accelerate
    if current_speed < effective_target:
        return self._calculate_power_notch(current_speed, effective_target)
    
    # Above target - brake
    return self._calculate_braking_notch_for_speed(current_speed, effective_target)
```

### 3.11 Class Definition

```python
@dataclass
class TrainController:
    """
    Combined ATC + ATO + command handling for one train.
    
    TrainController is the single authority for:
    - Power state and emergency stop
    - Active cab selection
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction
    - ATC speed limit calculation
    - ATO automatic driving decisions
    - Command validation
    
    It does NOT own:
    - Train position (owned by Train, updated by TrainPhysics)
    - Train current_speed (owned by Train, updated by TrainPhysics)
    - Block state (owned by BlockManager, updated by TrainPhysics)
    - Simulation time (owned by SimulationController)
    """
    
    train_id: str
    network: 'RailwayNetwork'
    
    # ─────────────────────────────────────────────────────────────────
    # Power & Safety State
    # ─────────────────────────────────────────────────────────────────
    power_on: bool = False
    emergency_stop_active: bool = False
    
    # ─────────────────────────────────────────────────────────────────
    # Cab & Mode State
    # ─────────────────────────────────────────────────────────────────
    active_cab: Optional[str] = None  # 'A', 'B', or None
    control_mode: str = 'ato'  # 'ato' or 'manual'
    travel_direction: str = 'forward'  # 'forward' or 'reverse'
    
    # ─────────────────────────────────────────────────────────────────
    # Operational State
    # ─────────────────────────────────────────────────────────────────
    state: TrainState = TrainState.OFF
    target_speed: float = 0.0
    handle_position: HandlePosition = HandlePosition.N
    
    # ─────────────────────────────────────────────────────────────────
    # ATO State
    # ─────────────────────────────────────────────────────────────────
    ato_running: bool = False
    ato_destination_id: Optional[str] = None
    ato_path_id: Optional[str] = None
    
    # ─────────────────────────────────────────────────────────────────
    # ATC State (calculated each tick)
    # ─────────────────────────────────────────────────────────────────
    clearance_ahead_cm: float = 0.0
    atc_speed_limit: float = 0.0
    current_block_speed_limit: float = float('inf')
    
    # ─────────────────────────────────────────────────────────────────
    # Tick Method
    # ─────────────────────────────────────────────────────────────────
    
    def tick(self, delta: float) -> None:
        """Called each simulation tick. Calculates ATC limits and ATO decisions."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Power Commands
    # ─────────────────────────────────────────────────────────────────
    
    def power_on_cmd(self) -> CommandResult:
        """Turn on train power. Train must be OFF."""
        ...
    
    def power_off_cmd(self) -> CommandResult:
        """Turn off train power. Train must be stopped."""
        ...
    
    def emergency_stop_cmd(self) -> CommandResult:
        """Activate emergency stop. Always allowed."""
        ...
    
    def release_emergency_cmd(self) -> CommandResult:
        """Release emergency stop. Must be stopped, handle in N/B."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Cab & Mode Commands
    # ─────────────────────────────────────────────────────────────────
    
    def set_cab(self, cab: Optional[str]) -> CommandResult:
        """Set active cab. Train must be stopped. Flips direction if A↔B."""
        ...
    
    def set_mode(self, mode: str) -> CommandResult:
        """Set control mode ('ato' or 'manual'). Train must be stopped."""
        ...
    
    def set_travel_direction(self, direction: str) -> CommandResult:
        """Set travel direction for manual reverse. Must be stopped."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Handle Commands
    # ─────────────────────────────────────────────────────────────────
    
    def set_handle(self, position: HandlePosition) -> CommandResult:
        """Set throttle/brake handle position."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # ATO Commands
    # ─────────────────────────────────────────────────────────────────
    
    def ato_go(self) -> CommandResult:
        """Start ATO movement. Must have cab set and be in STANDBY."""
        ...
    
    def ato_stop(self) -> CommandResult:
        """Stop ATO movement (service stop). Decelerates to stop."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # ATC Methods (internal)
    # ─────────────────────────────────────────────────────────────────
    
    def _calculate_clearance_ahead(self) -> float:
        """Calculate distance to next obstruction."""
        ...
    
    def _calculate_atc_speed_limit(self) -> float:
        """Calculate max safe speed based on clearance (braking curve)."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # ATO Methods (internal)
    # ─────────────────────────────────────────────────────────────────
    
    def _ato_calculate_handle(self) -> HandlePosition:
        """ATO: automatically select handle position for smooth driving."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────────────────────────
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize controller state for API/frontend."""
        return {
            'train_id': self.train_id,
            'power_on': self.power_on,
            'emergency_stop_active': self.emergency_stop_active,
            'active_cab': self.active_cab,
            'control_mode': self.control_mode,
            'travel_direction': self.travel_direction,
            'state': self.state.value,
            'target_speed': self.target_speed,
            'handle_position': self.handle_position.value,
            'ato_running': self.ato_running,
            'ato_destination_id': self.ato_destination_id,
            'clearance_ahead_cm': self.clearance_ahead_cm,
            'atc_speed_limit': self.atc_speed_limit,
        }
```

### 3.12 Integration with Train Entity

TrainController reads/writes certain Train properties:

| Access | Property | Description |
|--------|----------|-------------|
| Read | `train.current_speed` | To know if train is stopped |
| Read | `train.max_speed` | To limit target speed |
| Read | `train.current_path` | To calculate clearance ahead |
| Read | `train.position` | To determine current block |
| Write | `train.facing_direction` | When switching cabs |
| Write | `train.active_cab` | Mirrors `active_cab` for frontend |

### 3.13 Integration with TrainPhysics

TrainController provides state that TrainPhysics reads:

| Property | Used By TrainPhysics For |
|----------|--------------------------|
| `handle_position` | Calculate acceleration rate |
| `power_on` | Skip physics if powered off |
| `emergency_stop_active` | Apply emergency braking |
| `target_speed` | Clamp speed to this limit |

---

## 4. TrainPhysics Specification

### 4.1 Purpose

TrainPhysics handles the **physical execution** of train movement. While TrainController decides WHAT speed the train should have, TrainPhysics executes HOW the train moves:

- Converts handle position to acceleration/deceleration rates
- Updates train speed based on acceleration and time delta
- Moves train position along the track (head and tail)
- Manages block occupancy (occupy new blocks, release vacated blocks)
- Outputs voltage to BlockPowerController

### 4.2 Design Goals

| Goal | Description |
|------|-------------|
| **Pure Physics** | Only handles movement calculations and world interactions |
| **Reads from Controller** | Gets handle_position, power state from TrainController |
| **Writes to Train** | Updates position, current_speed on Train entity |
| **Writes to World** | Updates BlockManager states, BlockPowerController voltages |
| **No Decision Making** | Doesn't decide speed limits or handle positions |

### 4.3 Properties

| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | ID of the train this physics instance controls |
| `network` | `RailwayNetwork` | Reference to the network for track/block access |
| `controller` | `TrainController` | Reference to read handle_position, power state |

### 4.4 Tick Method

```python
def tick(self, delta: float) -> None:
    """
    Called each simulation tick by SimulationController.
    
    Executes physical movement based on TrainController state.
    
    Args:
        delta: Time elapsed since last tick (seconds)
    """
    train = self._get_train()
    controller = self.controller
    
    # 1. Skip if powered off
    if not controller.power_on:
        self._set_voltage_for_blocks(train, 0.0)
        return
    
    # 2. Get acceleration from handle position
    acceleration = self._get_acceleration_for_handle(
        controller.handle_position,
        self._get_speed_profile()
    )
    
    # 3. Update speed based on acceleration
    new_speed = train.current_speed + (acceleration * delta)
    
    # 4. Clamp speed to valid range [0, target_speed]
    new_speed = max(0.0, min(new_speed, controller.target_speed))
    
    # 5. If braking and reached 0, stay at 0
    if acceleration < 0 and new_speed <= 0:
        new_speed = 0.0
    
    train.current_speed = new_speed
    
    # 6. Calculate distance to move
    distance_cm = self._speed_to_distance(new_speed, delta)
    
    # 7. Move train position
    if distance_cm > 0:
        self._move_train(train, distance_cm)
    
    # 8. Update block occupancy
    self._update_block_occupancy(train)
    
    # 9. Output voltage to occupied blocks
    self._update_voltage_output(train, controller.target_speed)
```

### 4.5 Movement Logic

#### 4.5.1 Speed to Distance Conversion

```python
def _speed_to_distance(self, speed_kmh: float, delta: float) -> float:
    """
    Convert speed (km/h) and time (seconds) to distance (cm).
    
    Formula: distance_cm = speed_kmh * (100000/3600) * delta
    
    Where:
        1 km/h = 100000 cm / 3600 s ≈ 27.78 cm/s
    """
    cm_per_second = speed_kmh * (100000 / 3600)
    return cm_per_second * delta
```

#### 4.5.2 Moving the Train

```python
def _move_train(self, train: Train, distance_cm: float) -> None:
    """
    Move train forward by distance_cm along the track.
    
    Updates both head and tail positions, handling tile transitions.
    """
    position = train.position
    
    # Move head forward
    new_head_distance = position.end_a_distance_cm + distance_cm
    
    # Handle tile transition if head moves past tile boundary
    while new_head_distance >= self._get_tile_length_cm(position.end_a_tile):
        tile_length = self._get_tile_length_cm(position.end_a_tile)
        new_head_distance -= tile_length
        
        # Get next tile based on exit edge
        exit_edge = self._calculate_exit_edge(position.end_a_tile, position.end_a_entry_edge)
        next_tile = self._get_neighbor_tile(position.end_a_tile, exit_edge)
        
        if next_tile is None:
            # End of track - stop at tile boundary
            new_head_distance = tile_length
            train.current_speed = 0.0
            break
        
        # Move to next tile
        position.end_a_tile = next_tile
        position.end_a_entry_edge = self._opposite_edge(exit_edge)
    
    position.end_a_distance_cm = new_head_distance
    
    # Recalculate tail position based on train length
    self._update_tail_position(train)
```

#### 4.5.3 Tail Position Calculation

```python
def _update_tail_position(self, train: Train) -> None:
    """
    Calculate tail position by tracing backward from head by train length.
    
    The tail is always train.length_cm behind the head along the track.
    """
    position = train.position
    
    # Start at head position
    current_tile = position.end_a_tile
    current_entry = position.end_a_entry_edge
    remaining_length = train.length_cm
    
    # Subtract head's distance from entry (how far we are into this tile)
    remaining_length -= position.end_a_distance_cm
    
    # If train fits in current tile
    if remaining_length <= 0:
        position.end_b_tile = current_tile
        position.end_b_entry_edge = current_entry
        position.end_b_distance_cm = position.end_a_distance_cm - train.length_cm
        return
    
    # Trace backward through tiles
    while remaining_length > 0:
        # Go to previous tile (enter from exit edge, which is opposite of current entry)
        prev_edge = current_entry  # We entered from here, so prev tile is this direction
        prev_tile = self._get_neighbor_tile(current_tile, prev_edge)
        
        if prev_tile is None:
            # Start of track - tail is at entry edge of first tile
            position.end_b_tile = current_tile
            position.end_b_entry_edge = current_entry
            position.end_b_distance_cm = 0.0
            return
        
        # Move to previous tile
        current_tile = prev_tile
        current_entry = self._opposite_edge(prev_edge)
        
        tile_length = self._get_tile_length_cm(current_tile)
        remaining_length -= tile_length
    
    # Tail is in this tile
    position.end_b_tile = current_tile
    position.end_b_entry_edge = current_entry
    position.end_b_distance_cm = abs(remaining_length)  # Distance from entry edge
    
    # Update occupied tiles list
    position.occupied_tiles = self._calculate_occupied_tiles(train)
```

### 4.6 Block Management

#### 4.6.1 Update Block Occupancy

```python
def _update_block_occupancy(self, train: Train) -> None:
    """
    Update which blocks this train occupies based on head/tail positions.
    
    - Occupy new blocks as head enters them
    - Release old blocks as tail exits them
    """
    # Calculate current occupied blocks from position
    new_occupied = set(self._get_blocks_for_tiles(train.position.occupied_tiles))
    old_occupied = set(train.occupied_blocks) if train.occupied_blocks else set()
    
    # Occupy new blocks
    for block_id in new_occupied - old_occupied:
        self.network.block_manager.occupy_block(block_id, train.train_id)
    
    # Release old blocks (tail has exited)
    for block_id in old_occupied - new_occupied:
        self.network.block_manager.clear_block(block_id)
    
    # Update train's record
    train.occupied_blocks = list(new_occupied)
```

#### 4.6.2 Block State Transitions

When TrainPhysics interacts with BlockManager, the following state transitions occur:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        BLOCK STATE MACHINE                                   │
│                                                                             │
│   ┌────────┐   reserve()    ┌──────────┐   occupy()    ┌──────────┐         │
│   │  FREE  │ ──────────────►│ RESERVED │ ─────────────►│ OCCUPIED │         │
│   └────────┘                └──────────┘               └────┬─────┘         │
│       ▲                                                     │               │
│       │                                                     │ set_caution() │
│       │ clear()                                             ▼               │
│       │                     ┌──────────┐               ┌──────────┐         │
│       └─────────────────────│ CAUTION  │◄──────────────│  (tail   │         │
│                             └──────────┘    exiting)   │  exits)  │         │
│                                                        └──────────┘         │
│                                                                             │
│   Note: TrainPhysics typically uses occupy() and clear()                    │
│         Path reservation uses reserve() and release()                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.7 Voltage Output

#### 4.7.1 Update Voltage for Train

```python
def _update_voltage_output(self, train: Train, target_speed: float) -> None:
    """
    Output voltage to all blocks occupied by this train.
    
    Uses SpeedProfile to convert target speed to voltage.
    """
    profile = self._get_speed_profile()
    
    # Convert target speed to voltage
    voltage = profile.speed_to_voltage(target_speed)
    
    # Special case: if train should be stopped but not powered off
    if target_speed == 0 and self.controller.power_on:
        # Use lighting voltage (keeps lights on)
        voltage = profile.lighting_voltage
    
    # Set voltage for all occupied blocks
    for block_id in train.occupied_blocks:
        self.network.power_controller.set_block_voltage(block_id, voltage)

def _set_voltage_for_blocks(self, train: Train, voltage: float) -> None:
    """Set specific voltage for all occupied blocks."""
    for block_id in train.occupied_blocks:
        self.network.power_controller.set_block_voltage(block_id, voltage)
```

### 4.8 6-Bit Track Geometry

TrainPhysics uses the 6-bit track system for movement calculations:

#### 4.8.1 Exit Edge Calculation

```python
def _calculate_exit_edge(self, tile: Tuple[int, int], entry_edge: str) -> str:
    """
    Calculate exit edge based on track geometry and entry edge.
    
    Uses the 6-bit track system to determine valid paths through the tile.
    
    Args:
        tile: (x, y) coordinates
        entry_edge: Edge train entered from ('N', 'E', 'S', 'W')
    
    Returns:
        Exit edge ('N', 'E', 'S', 'W')
    """
    track = self.network.get_track(*tile)
    if not track:
        return None
    
    # Get all possible exits from entry edge based on track bits
    # Track bits encode which edges are connected
    possible_exits = track.get_exits_from(entry_edge)
    
    if not possible_exits:
        return None
    
    # If train has a path, follow the path
    train = self._get_train()
    if train.current_path:
        next_tile_in_path = self._get_next_tile_in_path(tile)
        if next_tile_in_path:
            # Determine which edge leads to the next tile
            for exit_edge in possible_exits:
                neighbor = self._get_neighbor_tile(tile, exit_edge)
                if neighbor == next_tile_in_path:
                    return exit_edge
    
    # Default: use first available exit (for simple through tracks)
    return possible_exits[0] if possible_exits else None
```

#### 4.8.2 Opposite Edge

```python
OPPOSITE_EDGES = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}

def _opposite_edge(self, edge: str) -> str:
    """Get opposite edge (N↔S, E↔W)."""
    return OPPOSITE_EDGES[edge]
```

### 4.9 Class Definition

```python
@dataclass
class TrainPhysics:
    """
    Physical movement, block management, and voltage output for one train.
    
    TrainPhysics executes the physical simulation:
    - Converts handle position to acceleration/deceleration
    - Updates train speed and position
    - Manages block occupancy (occupy/release)
    - Outputs voltage to BlockPowerController
    
    It does NOT decide:
    - Speed limits (that's TrainController/ATC)
    - Handle position (that's TrainController/ATO)
    - When to start/stop (that's TrainController)
    """
    
    train_id: str
    network: 'RailwayNetwork'
    controller: 'TrainController'
    
    # ─────────────────────────────────────────────────────────────────
    # Main Tick Method
    # ─────────────────────────────────────────────────────────────────
    
    def tick(self, delta: float) -> None:
        """Execute physical movement for this simulation tick."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Speed & Acceleration
    # ─────────────────────────────────────────────────────────────────
    
    def _get_acceleration_for_handle(
        self, 
        handle: HandlePosition, 
        profile: SpeedProfile
    ) -> float:
        """Get acceleration rate (km/h/s) for handle position."""
        ...
    
    def _speed_to_distance(self, speed_kmh: float, delta: float) -> float:
        """Convert speed and time to distance in cm."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Movement
    # ─────────────────────────────────────────────────────────────────
    
    def _move_train(self, train: Train, distance_cm: float) -> None:
        """Move train forward by distance along track."""
        ...
    
    def _update_tail_position(self, train: Train) -> None:
        """Recalculate tail position based on head and train length."""
        ...
    
    def _calculate_exit_edge(self, tile: Tuple[int, int], entry_edge: str) -> str:
        """Calculate exit edge using 6-bit track geometry."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Block Management
    # ─────────────────────────────────────────────────────────────────
    
    def _update_block_occupancy(self, train: Train) -> None:
        """Update occupied blocks as train moves."""
        ...
    
    def _get_blocks_for_tiles(self, tiles: List[Tuple[int, int]]) -> List[str]:
        """Get block IDs for a list of tile coordinates."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Voltage Output
    # ─────────────────────────────────────────────────────────────────
    
    def _update_voltage_output(self, train: Train, target_speed: float) -> None:
        """Output voltage to occupied blocks based on target speed."""
        ...
    
    def _set_voltage_for_blocks(self, train: Train, voltage: float) -> None:
        """Set specific voltage for all occupied blocks."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────
    
    def _get_train(self) -> Train:
        """Get the Train entity."""
        return self.network.trains.get(self.train_id)
    
    def _get_speed_profile(self) -> SpeedProfile:
        """Get the SpeedProfile for this train."""
        ...
    
    def _get_tile_length_cm(self, tile: Tuple[int, int]) -> float:
        """Get the track length of a tile in cm."""
        ...
    
    def _get_neighbor_tile(self, tile: Tuple[int, int], edge: str) -> Optional[Tuple[int, int]]:
        """Get neighboring tile in the specified direction."""
        ...
```

---

## 5. Train Entity Specification

### 5.1 Purpose

The Train entity is a **data class** that stores the physical properties and state of a train. It is intentionally kept simple - a data container that TrainController and TrainPhysics read from and write to.

### 5.2 Design Philosophy

| Principle | Description |
|-----------|-------------|
| **Data Only** | Train stores data, doesn't make decisions |
| **No Business Logic** | Movement logic is in TrainPhysics, control logic is in TrainController |
| **Passive Updates** | TrainPhysics writes position/speed, TrainController writes cab/mode state |
| **Convenience Methods** | Simple helpers like `is_stopped()` are allowed |

### 5.3 Properties

#### 5.3.1 Identity

| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | Unique identifier |
| `name` | `str` | Display name |
| `model` | `str` | Model name (e.g., "BR 218", "Shinkansen E5") |

#### 5.3.2 Physical Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `length_cm` | `float` | 50.0 | Physical length in cm |
| `max_speed` | `float` | 100.0 | Maximum speed in km/h |
| `min_radius_cm` | `float` | 30.0 | Minimum curve radius train can navigate |
| `speed_profile_id` | `Optional[str]` | None | ID of SpeedProfile (uses DEFAULT if None) |

#### 5.3.3 Position & Movement

| Property | Type | Description |
|----------|------|-------------|
| `position` | `TrainPosition` | Full position tracking (head, tail, occupied tiles) |
| `current_speed` | `float` | Actual speed in km/h (updated by TrainPhysics) |
| `facing_direction` | `Direction` | Which way train is facing (N/E/S/W) |
| `active_cab` | `Optional[str]` | Active cab ('A', 'B', None) - mirrored from controller |

#### 5.3.4 Path & Blocks

| Property | Type | Description |
|----------|------|-------------|
| `current_path` | `Optional[List[Tuple[int, int]]]` | Path tiles from pathfinder |
| `path_index` | `int` | Current position in path |
| `occupied_blocks` | `List[str]` | Block IDs currently occupied |

### 5.4 TrainPosition Dataclass

TrainPosition tracks both ends of a train independently:

```python
@dataclass
class TrainPosition:
    """
    Precise position tracking for both ends of a train.
    
    Design Goals:
    - Track head (end_a) and tail (end_b) independently
    - Support trains spanning multiple tiles
    - Enable accurate visualization on each occupied tile
    - Handle reversal by swapping leading_end
    """
    
    # End A position (typically the head when moving forward)
    end_a_tile: Tuple[int, int]        # (x, y) tile coordinates
    end_a_distance_cm: float           # Distance from entry edge (cm)
    end_a_entry_edge: str              # Edge where this end entered ('N','E','S','W')
    
    # End B position (typically the tail when moving forward)
    end_b_tile: Tuple[int, int]
    end_b_distance_cm: float
    end_b_entry_edge: str
    
    # Which end is leading (swaps on reverse)
    leading_end: str = "a"             # "a" or "b"
    
    # Direction train is facing
    facing_direction: Direction = None
    
    # Computed from position
    occupied_tiles: List[Tuple[int, int]] = None
    occupied_blocks: List[str] = None
    
    # ─────────────────────────────────────────────────────────────────
    # Head/Tail Properties (delegate based on leading_end)
    # ─────────────────────────────────────────────────────────────────
    
    @property
    def head_tile(self) -> Tuple[int, int]:
        """Get the tile where the head is located."""
        return self.end_a_tile if self.leading_end == "a" else self.end_b_tile
    
    @property
    def tail_tile(self) -> Tuple[int, int]:
        """Get the tile where the tail is located."""
        return self.end_b_tile if self.leading_end == "a" else self.end_a_tile
    
    @property
    def head_distance_cm(self) -> float:
        """Get head's distance from entry edge."""
        return self.end_a_distance_cm if self.leading_end == "a" else self.end_b_distance_cm
    
    @property
    def tail_distance_cm(self) -> float:
        """Get tail's distance from entry edge."""
        return self.end_b_distance_cm if self.leading_end == "a" else self.end_a_distance_cm
```

### 5.5 Convenience Methods

```python
class Train:
    # ... properties ...
    
    def is_stopped(self) -> bool:
        """Check if train is stopped (speed ≈ 0)."""
        return self.current_speed < STOPPED_SPEED_THRESHOLD_KMH  # 0.1 km/h
    
    def is_placed(self) -> bool:
        """Check if train has been placed on track."""
        return self.position is not None
    
    def get_head_tile(self) -> Optional[Tuple[int, int]]:
        """Get the tile where the head is located."""
        return self.position.head_tile if self.position else None
    
    def get_tail_tile(self) -> Optional[Tuple[int, int]]:
        """Get the tile where the tail is located."""
        return self.position.tail_tile if self.position else None
```

### 5.6 Serialization (to_dict)

```python
def to_dict(self, network=None) -> Dict[str, Any]:
    """
    Serialize train state for API/frontend.
    
    Args:
        network: RailwayNetwork for calculating tile lengths (optional)
    
    Returns:
        Dictionary with all train properties
    """
    data = {
        # Identity
        'train_id': self.train_id,
        'name': self.name,
        'model': self.model,
        
        # Physical properties
        'length_cm': self.length_cm,
        'max_speed': self.max_speed,
        'min_radius_cm': self.min_radius_cm,
        'speed_profile_id': self.speed_profile_id,
        
        # Movement
        'current_speed': self.current_speed,
        'facing_direction': self.facing_direction.name if self.facing_direction else None,
        'active_cab': self.active_cab,
        
        # Path
        'current_path': self.current_path,
        'path_index': self.path_index,
        'occupied_blocks': self.occupied_blocks,
    }
    
    # Position (new format)
    if self.position:
        data['position'] = {
            'end_a': {
                'tile': list(self.position.end_a_tile),
                'distance_cm': self.position.end_a_distance_cm,
                'entry_edge': self.position.end_a_entry_edge,
            },
            'end_b': {
                'tile': list(self.position.end_b_tile),
                'distance_cm': self.position.end_b_distance_cm,
                'entry_edge': self.position.end_b_entry_edge,
            },
            'leading_end': self.position.leading_end,
            'facing_direction': self.position.facing_direction.name if self.position.facing_direction else None,
            'occupied_tiles': [list(t) for t in self.position.occupied_tiles] if self.position.occupied_tiles else [],
        }
        
        # Legacy position format (for backward compatible frontends)
        if network and self.position.end_a_tile:
            tile_length_cm = network.get_tile_length_cm(*self.position.end_a_tile)
            if tile_length_cm > 0:
                data['position_legacy'] = {
                    'x': self.position.end_a_tile[0],
                    'y': self.position.end_a_tile[1],
                    'distance': self.position.end_a_distance_cm / tile_length_cm,  # 0-1 ratio
                    'tile_length_cm': tile_length_cm,
                    'entry_edge': self.position.end_a_entry_edge,
                }
    
    return data
```

### 5.7 What Train Does NOT Store

These properties are stored in TrainController, not Train:

| Property | Lives In | Reason |
|----------|----------|--------|
| `state` (OFF/IDLE/etc) | TrainController | Operational state, not physical |
| `target_speed` | TrainController | Decision output, not physical reality |
| `handle_position` | TrainController | Control input, not physical property |
| `power_on` | TrainController | Operational state |
| `ato_running` | TrainController | Operational state |
| `control_mode` | TrainController | Operational state |

### 5.8 Class Definition

```python
@dataclass
class Train:
    """
    Train data entity - stores physical properties and state.
    
    Train is a passive data container. It does not make decisions.
    TrainController makes control decisions.
    TrainPhysics updates position and speed.
    """
    
    # ─────────────────────────────────────────────────────────────────
    # Identity
    # ─────────────────────────────────────────────────────────────────
    train_id: str
    name: str = ""
    model: str = ""
    
    # ─────────────────────────────────────────────────────────────────
    # Physical Properties
    # ─────────────────────────────────────────────────────────────────
    length_cm: float = 50.0
    max_speed: float = 100.0
    min_radius_cm: float = 30.0
    speed_profile_id: Optional[str] = None
    
    # ─────────────────────────────────────────────────────────────────
    # Position & Movement (updated by TrainPhysics)
    # ─────────────────────────────────────────────────────────────────
    position: Optional[TrainPosition] = None
    current_speed: float = 0.0
    facing_direction: Optional[Direction] = None
    active_cab: Optional[str] = None  # Mirrored from controller for frontend
    
    # ─────────────────────────────────────────────────────────────────
    # Path & Blocks
    # ─────────────────────────────────────────────────────────────────
    current_path: Optional[List[Tuple[int, int]]] = None
    path_index: int = 0
    occupied_blocks: List[str] = field(default_factory=list)
    
    # ─────────────────────────────────────────────────────────────────
    # Convenience Methods
    # ─────────────────────────────────────────────────────────────────
    
    def is_stopped(self) -> bool:
        """Check if train is stopped."""
        ...
    
    def is_placed(self) -> bool:
        """Check if train has been placed on track."""
        ...
    
    def get_head_tile(self) -> Optional[Tuple[int, int]]:
        """Get head tile coordinates."""
        ...
    
    def get_tail_tile(self) -> Optional[Tuple[int, int]]:
        """Get tail tile coordinates."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────────────────────────
    
    def to_dict(self, network=None) -> Dict[str, Any]:
        """Serialize for API/frontend."""
        ...
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Train':
        """Deserialize from dictionary."""
        ...
```

---

## 6. SpeedProfile & Handle Model

### 6.1 Purpose

SpeedProfile maps a train's target speed (km/h) to the voltage that should be output to the blocks it occupies. It also defines acceleration and deceleration rates used by the handle model.

### 6.2 SpeedProfile Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `profile_id` | `str` | Auto | Unique identifier |
| `name` | `str` | - | Display name (e.g., "HO Generic", "N Scale Slow") |
| `speed_voltage_curve` | `List[Tuple[float, float]]` | - | Calibration points: (speed_kmh, voltage) |
| `min_start_voltage` | `float` | 2.0 | Minimum voltage to overcome static friction |
| `lighting_voltage` | `float` | 2.5 | Voltage for lights without movement |
| `acceleration_rate` | `float` | 10.0 | Speed increase rate (km/h per second) |
| `deceleration_rate` | `float` | 15.0 | Service brake rate (km/h per second) |
| `emergency_brake_rate` | `float` | 30.0 | Emergency brake rate (km/h per second) |

### 6.3 Speed-Voltage Curve

The curve is defined as calibration points. The system interpolates between points:

```python
# Example: HO Scale Generic Profile
speed_voltage_curve = [
    (0.0, 0.0),      # Stopped
    (10.0, 3.0),     # 10 km/h requires 3V
    (30.0, 5.0),     # 30 km/h requires 5V
    (60.0, 8.0),     # 60 km/h requires 8V
    (100.0, 11.0),   # 100 km/h requires 11V
    (120.0, 12.0),   # Max speed at max voltage
]
```

### 6.4 Interpolation Methods

```python
def speed_to_voltage(self, speed_kmh: float) -> float:
    """
    Convert target speed to required voltage (interpolated).
    
    Uses linear interpolation between calibration points.
    """
    if speed_kmh <= 0:
        return 0.0
    
    curve = self.speed_voltage_curve
    
    # Find surrounding points
    for i in range(len(curve) - 1):
        (s1, v1), (s2, v2) = curve[i], curve[i + 1]
        if s1 <= speed_kmh <= s2:
            # Linear interpolation
            t = (speed_kmh - s1) / (s2 - s1)
            return v1 + t * (v2 - v1)
    
    # Beyond curve - use last voltage
    return curve[-1][1]

def voltage_to_speed(self, voltage: float) -> float:
    """
    Convert voltage to expected speed (interpolated).
    
    Inverse of speed_to_voltage.
    """
    if voltage <= 0:
        return 0.0
    
    curve = self.speed_voltage_curve
    
    for i in range(len(curve) - 1):
        (s1, v1), (s2, v2) = curve[i], curve[i + 1]
        if v1 <= voltage <= v2:
            t = (voltage - v1) / (v2 - v1)
            return s1 + t * (s2 - s1)
    
    return curve[-1][0]
```

### 6.5 Handle Model

Real trains are not driven by setting a target speed directly. Instead, the driver uses a **combined throttle/brake handle** with discrete notches:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        THROTTLE/BRAKE HANDLE                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  POWER (Accelerate)                                                     │
│  ───────────────────                                                    │
│    P5  ████████████████████  100% acceleration_rate                     │
│    P4  ████████████████      80% acceleration_rate                      │
│    P3  ████████████          60% acceleration_rate                      │
│    P2  ████████              40% acceleration_rate                      │
│    P1  ████                  20% acceleration_rate                      │
│                                                                         │
│  NEUTRAL (Coast)                                                        │
│  ───────────────                                                        │
│    N   ─────────────────────  No power, no braking (coast)              │
│                                                                         │
│  BRAKE (Decelerate)                                                     │
│  ──────────────────                                                     │
│    B1  ████                  12.5% deceleration_rate                    │
│    B2  ████████              25% deceleration_rate                      │
│    B3  ████████████          37.5% deceleration_rate                    │
│    B4  ████████████████      50% deceleration_rate                      │
│    B5  ████████████████████  62.5% deceleration_rate                    │
│    B6  ████████████████████████  75% deceleration_rate                  │
│    B7  ████████████████████████████  87.5% deceleration_rate            │
│    B8  ████████████████████████████████  100% deceleration_rate         │
│                                                                         │
│  EMERGENCY                                                              │
│  ─────────                                                              │
│    EB  ████████████████████████████████████  emergency_brake_rate       │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 6.6 HandlePosition Enum

```python
class HandlePosition(Enum):
    """Throttle/brake handle positions."""
    P5 = "P5"  # Power notch 5 (max acceleration)
    P4 = "P4"  # Power notch 4
    P3 = "P3"  # Power notch 3
    P2 = "P2"  # Power notch 2
    P1 = "P1"  # Power notch 1 (min acceleration)
    N = "N"    # Neutral (coast)
    B1 = "B1"  # Brake notch 1 (min braking)
    B2 = "B2"  # Brake notch 2
    B3 = "B3"  # Brake notch 3
    B4 = "B4"  # Brake notch 4
    B5 = "B5"  # Brake notch 5
    B6 = "B6"  # Brake notch 6
    B7 = "B7"  # Brake notch 7
    B8 = "B8"  # Brake notch 8 (max service braking)
    EB = "EB"  # Emergency brake

# Handle position order (for UI slider)
HANDLE_ORDER = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB']
```

### 6.7 Handle to Acceleration Conversion

```python
def get_acceleration_for_handle(
    handle: HandlePosition,
    speed_profile: SpeedProfile
) -> float:
    """
    Get acceleration/deceleration rate for current handle position.
    
    Returns:
        Positive value for acceleration (km/h/s)
        Negative value for deceleration (km/h/s)
        Zero for neutral (coast)
    """
    accel_rate = speed_profile.acceleration_rate
    decel_rate = speed_profile.deceleration_rate
    emergency_rate = speed_profile.emergency_brake_rate
    
    # Power notches (acceleration)
    power_multipliers = {
        HandlePosition.P5: 1.0,
        HandlePosition.P4: 0.8,
        HandlePosition.P3: 0.6,
        HandlePosition.P2: 0.4,
        HandlePosition.P1: 0.2,
    }
    
    # Brake notches (deceleration)
    brake_multipliers = {
        HandlePosition.B1: 0.125,
        HandlePosition.B2: 0.25,
        HandlePosition.B3: 0.375,
        HandlePosition.B4: 0.5,
        HandlePosition.B5: 0.625,
        HandlePosition.B6: 0.75,
        HandlePosition.B7: 0.875,
        HandlePosition.B8: 1.0,
    }
    
    if handle in power_multipliers:
        return accel_rate * power_multipliers[handle]
    elif handle == HandlePosition.N:
        return 0.0  # Coast
    elif handle in brake_multipliers:
        return -decel_rate * brake_multipliers[handle]
    elif handle == HandlePosition.EB:
        return -emergency_rate
    
    return 0.0
```

### 6.8 Speed Update from Handle

```python
def update_speed_from_handle(
    current_speed: float,
    handle: HandlePosition,
    speed_profile: SpeedProfile,
    delta_seconds: float,
    max_speed: float,
    atc_limit: float
) -> float:
    """
    Calculate new speed based on handle position and time elapsed.
    
    Args:
        current_speed: Current speed in km/h
        handle: Current handle position
        speed_profile: Train's speed profile
        delta_seconds: Time since last update
        max_speed: Train's maximum speed
        atc_limit: ATC-enforced speed limit
        
    Returns:
        New speed in km/h
    """
    acceleration = get_acceleration_for_handle(handle, speed_profile)
    
    # Calculate effective speed limit (min of max_speed and atc_limit)
    effective_limit = min(max_speed, atc_limit)
    
    # Apply acceleration/deceleration
    new_speed = current_speed + (acceleration * delta_seconds)
    
    # Clamp to valid range
    new_speed = max(0.0, min(new_speed, effective_limit))
    
    # If accelerating and at/above limit, don't accelerate further
    if acceleration > 0 and current_speed >= effective_limit:
        new_speed = current_speed
    
    return new_speed
```

### 6.9 SpeedProfile Class Definition

```python
@dataclass
class SpeedProfile:
    """
    Maps target speed to voltage output for DC analog control.
    
    Also defines acceleration/deceleration rates for the handle model.
    """
    
    profile_id: str
    name: str
    speed_voltage_curve: List[Tuple[float, float]]  # [(speed_kmh, voltage), ...]
    
    min_start_voltage: float = 2.0      # Voltage to overcome static friction
    lighting_voltage: float = 2.5       # Voltage for lights without movement
    acceleration_rate: float = 10.0     # km/h per second
    deceleration_rate: float = 15.0     # km/h per second (service brake)
    emergency_brake_rate: float = 30.0  # km/h per second
    
    # ─────────────────────────────────────────────────────────────────
    # Speed ↔ Voltage Conversion
    # ─────────────────────────────────────────────────────────────────
    
    def speed_to_voltage(self, speed_kmh: float) -> float:
        """Convert target speed to required voltage (interpolated)."""
        ...
    
    def voltage_to_speed(self, voltage: float) -> float:
        """Convert voltage to expected speed (interpolated)."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────────────────────────
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API/storage."""
        return {
            'profile_id': self.profile_id,
            'name': self.name,
            'speed_voltage_curve': self.speed_voltage_curve,
            'min_start_voltage': self.min_start_voltage,
            'lighting_voltage': self.lighting_voltage,
            'acceleration_rate': self.acceleration_rate,
            'deceleration_rate': self.deceleration_rate,
            'emergency_brake_rate': self.emergency_brake_rate,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SpeedProfile':
        """Deserialize from dictionary."""
        return cls(
            profile_id=data['profile_id'],
            name=data['name'],
            speed_voltage_curve=[tuple(p) for p in data['speed_voltage_curve']],
            min_start_voltage=data.get('min_start_voltage', 2.0),
            lighting_voltage=data.get('lighting_voltage', 2.5),
            acceleration_rate=data.get('acceleration_rate', 10.0),
            deceleration_rate=data.get('deceleration_rate', 15.0),
            emergency_brake_rate=data.get('emergency_brake_rate', 30.0),
        )
```

### 6.10 Default SpeedProfile

A default profile is created automatically for trains without a specific profile:

```python
DEFAULT_SPEED_PROFILE = SpeedProfile(
    profile_id="DEFAULT",
    name="Default Profile",
    speed_voltage_curve=[
        (0.0, 0.0),
        (10.0, 3.0),
        (30.0, 5.0),
        (60.0, 8.0),
        (100.0, 11.0),
        (120.0, 12.0),
    ],
    min_start_voltage=2.0,
    lighting_voltage=2.5,
    acceleration_rate=10.0,
    deceleration_rate=15.0,
    emergency_brake_rate=30.0,
)
```

---

## 7. Route & Waypoint Specification

### 7.1 Purpose

A `Route` is the train's travel plan - a sequence of waypoints (stations) the train will visit autonomously. This is distinct from the dispatcher's `Path` (infrastructure reservation).

| Term | Definition |
|------|------------|
| **Route** | Train's travel plan (waypoints, timing, mode) |
| **Path** | Infrastructure reservation (blocks, signals, interlocking) |
| **Waypoint** | Single stop in a Route (station, dwell time, timing) |

### 7.2 Route Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `route_id` | `str` | Auto | Unique identifier |
| `name` | `str` | "" | Display name |
| `waypoints` | `List[Waypoint]` | [] | Ordered list of stops |
| `mode` | `RouteMode` | ONE_WAY | How to traverse waypoints |
| `current_waypoint_index` | `int` | 0 | Current position in waypoints |
| `direction` | `int` | 1 | +1 = forward, -1 = reverse (for ROUND_TRIP) |
| `is_active` | `bool` | False | Whether route is being executed |
| `started_at` | `Optional[float]` | None | Simulation time when activated |
| `last_error` | `Optional[str]` | None | Last error message |

### 7.3 RouteMode Enum

| Value | Behavior | Example (waypoints: A, B, C) |
|-------|----------|------------------------------|
| `ONE_WAY` | Visit once, then stop | A → B → C → (done) |
| `LOOP` | Return to start, repeat | A → B → C → A → B → C → ... |
| `ROUND_TRIP` | Reverse at ends, repeat | A → B → C → B → A → B → ... |

```python
class RouteMode(Enum):
    ONE_WAY = "one_way"         # A → B → C → stop
    LOOP = "loop"               # A → B → C → A → B → C → ...
    ROUND_TRIP = "round_trip"   # A → B → C → B → A → B → ...
```

### 7.4 Waypoint Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `station_id` | `str` | - | Target station ID |
| `dwell_time_sec` | `float` | 30.0 | Default stop duration |
| `timing_mode` | `TimingMode` | NONE | How timing is applied |
| `target_time` | `Optional[float]` | None | For ABSOLUTE/RELATIVE modes |

### 7.5 TimingMode Enum

| Value | Description | `target_time` meaning |
|-------|-------------|----------------------|
| `NONE` | No timing constraint | Ignored; depart after dwell_time |
| `RELATIVE` | Relative to route start | Seconds since route activation |
| `ABSOLUTE` | Synchronized time | Simulation clock time |

```python
class TimingMode(Enum):
    NONE = "none"           # No timing, just dwell
    RELATIVE = "relative"   # Time since route started
    ABSOLUTE = "absolute"   # Synchronized simulation time
```

### 7.6 Automatic Turn-Around (Cab Switch Model)

When a train arrives at a waypoint and needs to travel in the opposite direction to reach the next waypoint, the system automatically performs a cab switch:

```
BEFORE turn_around():
  facing_direction = WEST
  travel_direction = 'forward'
  → Train moves WEST

AFTER turn_around():
  facing_direction = EAST  (flipped!)
  travel_direction = 'forward'  (unchanged)
  → Train moves EAST
```

**Key Points:**
- `facing_direction` **flips** to the new direction (like driver moving to other cab)
- `travel_direction` stays `'forward'` (train always moves "forward" relative to active cab)
- `travel_direction` is only `'reverse'` in MANUAL mode when user wants to drive backward

### 7.7 Route Class Definition

```python
@dataclass
class Route:
    """Train's travel plan - sequence of waypoints to visit."""
    
    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_WAY
    
    # Execution state
    current_waypoint_index: int = 0
    direction: int = 1  # +1 forward, -1 reverse (for ROUND_TRIP)
    is_active: bool = False
    started_at: Optional[float] = None
    last_error: Optional[str] = None
    error_time: Optional[float] = None
    
    def get_current_waypoint(self) -> Optional[Waypoint]:
        """Get the current target waypoint."""
        if 0 <= self.current_waypoint_index < len(self.waypoints):
            return self.waypoints[self.current_waypoint_index]
        return None
    
    def advance_to_next_waypoint(self) -> Optional[Waypoint]:
        """
        Move to next waypoint based on mode. 
        Returns new waypoint or None if route complete.
        """
        if self.mode == RouteMode.ONE_WAY:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                return None  # Route complete
        
        elif self.mode == RouteMode.LOOP:
            self.current_waypoint_index += 1
            if self.current_waypoint_index >= len(self.waypoints):
                self.current_waypoint_index = 0  # Wrap around
        
        elif self.mode == RouteMode.ROUND_TRIP:
            self.current_waypoint_index += self.direction
            if self.current_waypoint_index >= len(self.waypoints):
                self.direction = -1
                self.current_waypoint_index = len(self.waypoints) - 2
            elif self.current_waypoint_index < 0:
                self.direction = 1
                self.current_waypoint_index = 1
        
        return self.get_current_waypoint()
    
    def is_complete(self) -> bool:
        """Check if route has completed (ONE_WAY only)."""
        if self.mode != RouteMode.ONE_WAY:
            return False
        return self.current_waypoint_index >= len(self.waypoints)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API/storage."""
        return {
            'route_id': self.route_id,
            'name': self.name,
            'waypoints': [w.to_dict() for w in self.waypoints],
            'mode': self.mode.value,
            'current_waypoint_index': self.current_waypoint_index,
            'direction': self.direction,
            'is_active': self.is_active,
            'started_at': self.started_at,
            'last_error': self.last_error,
        }
```

### 7.8 Waypoint Class Definition

```python
@dataclass
class Waypoint:
    """Single stop in a train's Route."""
    
    station_id: str
    dwell_time_sec: float = 30.0
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None
    
    def should_depart(
        self, 
        time_at_station: float, 
        route_start_time: float,
        simulation_time: float
    ) -> bool:
        """Determine if train should depart based on timing mode."""
        if self.timing_mode == TimingMode.NONE:
            return time_at_station >= self.dwell_time_sec
        elif self.timing_mode == TimingMode.RELATIVE:
            elapsed = simulation_time - route_start_time
            return elapsed >= self.target_time
        elif self.timing_mode == TimingMode.ABSOLUTE:
            return simulation_time >= self.target_time
        return False
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API/storage."""
        return {
            'station_id': self.station_id,
            'dwell_time_sec': self.dwell_time_sec,
            'timing_mode': self.timing_mode.value,
            'target_time': self.target_time,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Waypoint':
        """Deserialize from dictionary."""
        return cls(
            station_id=data['station_id'],
            dwell_time_sec=data.get('dwell_time_sec', 30.0),
            timing_mode=TimingMode(data.get('timing_mode', 'none')),
            target_time=data.get('target_time'),
        )
```

### 7.9 Error Handling: Path Not Found

When a train's Route cannot find a Path to the next waypoint:

1. **Train stops** at current position (target_speed = 0)
2. **Error is logged** with details (train_id, current position, target station)
3. **Train state** set to `RUNNING_WAITING` with wait_reason
4. **Route remains active** but paused - RouteExecutor will retry
5. **UI displays error** to operator via wait_reason

**Automatic Recovery:** When in RUNNING_WAITING, the controller's `tick()` method continuously checks if the path has cleared (ATC speed limit > 0). When the obstruction clears:
- State automatically transitions to RUNNING_MOVING
- ATO is restarted if in ATO mode
- Train resumes toward destination

This means temporary obstructions (another train in the block, signal timing) are handled automatically without operator intervention.

```python
def _dispatch_to_station(self, train_id: str, station_id: str) -> bool:
    """Request a path to the target station."""
    controller = self.controllers[train_id]
    train = self.network.trains[train_id]
    
    station = self.network.stations.get(station_id)
    if not station:
        self._set_route_error(train_id, f"Station {station_id} not found")
        return False
    
    # Try to create path via PathDispatcher
    result = self.path_dispatcher.create_path(
        start_block_id=train.occupied_blocks[0] if train.occupied_blocks else None,
        end_station_id=station_id,
        train_id=train_id
    )
    
    if not result.success:
        self._set_route_error(train_id, f"No path to {station_id}: {result.error}")
        controller.wait_for_path(f"No path to {station_id}")
        return False
    
    train.current_path = result.path.tiles
    controller.ato_path_id = result.path.path_id
    controller.state = TrainState.RUNNING_MOVING
    return True
```

---

## 8. Block & Power Control

### 8.1 BlockManager

BlockManager is the **single authority** for block state changes. It enforces the block state machine and prevents race conditions.

#### 8.1.1 Purpose

| Responsibility | Description |
|----------------|-------------|
| State Machine | Enforces: FREE → RESERVED → OCCUPIED → CAUTION → FREE |
| Atomic Operations | All state changes through validated methods |
| Conflict Detection | Prevents double-occupancy |
| Single Authority | Only BlockManager modifies block states |

#### 8.1.2 Block States

| State | Description | Who Sets It |
|-------|-------------|-------------|
| `FREE` | Block is available | BlockManager.clear_block() |
| `RESERVED` | Block is reserved for a path | PathDispatcher via BlockManager |
| `OCCUPIED` | Train is physically in block | TrainPhysics via BlockManager |
| `CAUTION` | Train is exiting, approach with caution | TrainPhysics via BlockManager |

#### 8.1.3 State Machine

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           BLOCK STATE MACHINE                                │
└─────────────────────────────────────────────────────────────────────────────┘

                    Path requests block
                           │
                           ▼
    ┌────────┐   reserve()   ┌──────────┐
    │  FREE  │ ─────────────►│ RESERVED │
    └────────┘               └────┬─────┘
        ▲                        │
        │                        │ Train head enters
        │                        │ occupy_block()
        │                        ▼
        │                   ┌──────────┐
        │                   │ OCCUPIED │
        │                   └────┬─────┘
        │                        │
        │                        │ Train tail exiting
        │                        │ set_block_to_caution()
        │                        ▼
        │   clear_block()   ┌──────────┐
        └───────────────────│ CAUTION  │
                            └──────────┘
                                 │
                                 │ Train tail fully exits
                                 │ clear_block()
                                 ▼
                            ┌────────┐
                            │  FREE  │
                            └────────┘
```

#### 8.1.4 BlockManager Methods

```python
@dataclass
class BlockManager:
    """Centralized block state manager - single source of truth."""
    
    network: 'RailwayNetwork'
    
    def reserve_block(self, block_id: str, path_id: str) -> bool:
        """
        Reserve block for a path.
        
        Rules:
        - Block must be FREE
        - Sets state to RESERVED
        """
        block = self.network.blocks.get(block_id)
        if not block:
            return False
        
        if block.state != BlockState.FREE:
            return False
        
        block.state = BlockState.RESERVED
        block.reserved_by = path_id
        return True
    
    def occupy_block(self, block_id: str, train_id: str) -> bool:
        """
        Mark block as occupied by train.
        
        Rules:
        - Block must be FREE or RESERVED (for this train's path)
        - Sets state to OCCUPIED
        """
        block = self.network.blocks.get(block_id)
        if not block:
            return False
        
        if block.state == BlockState.OCCUPIED:
            # Already occupied - check if same train (idempotent)
            return block.occupied_by == train_id
        
        if block.state not in [BlockState.FREE, BlockState.RESERVED]:
            return False
        
        block.state = BlockState.OCCUPIED
        block.occupied_by = train_id
        return True
    
    def set_block_to_caution(self, block_id: str) -> bool:
        """
        Set block to CAUTION state (train tail exiting).
        
        Rules:
        - Block must be OCCUPIED
        - Sets state to CAUTION
        """
        block = self.network.blocks.get(block_id)
        if not block:
            return False
        
        if block.state != BlockState.OCCUPIED:
            return False
        
        block.state = BlockState.CAUTION
        return True
    
    def clear_block(self, block_id: str) -> bool:
        """
        Clear block completely (train fully exited).
        
        Rules:
        - Block must be OCCUPIED or CAUTION
        - Sets state to FREE
        """
        block = self.network.blocks.get(block_id)
        if not block:
            return False
        
        if block.state not in [BlockState.OCCUPIED, BlockState.CAUTION]:
            return False
        
        block.state = BlockState.FREE
        block.occupied_by = None
        block.reserved_by = None
        return True
    
    def release_block(self, block_id: str) -> bool:
        """
        Release reservation (path cancelled before train entered).
        
        Rules:
        - Block must be RESERVED
        - Sets state to FREE
        """
        block = self.network.blocks.get(block_id)
        if not block:
            return False
        
        if block.state != BlockState.RESERVED:
            return False
        
        block.state = BlockState.FREE
        block.reserved_by = None
        return True
```

### 8.2 BlockPowerController

BlockPowerController manages voltage output to track blocks for DC analog control.

#### 8.2.1 Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                       BlockPowerController                                  │
│                                                                            │
│   ┌──────────────────┐    ┌──────────────────────┐                         │
│   │  Block Voltage   │    │   Output Interface   │                         │
│   │  Registry        │    │   (Abstract)         │                         │
│   │  ────────────    │    │   ─────────────────  │                         │
│   │  BLK_001: 8.0V   │───►│   set_voltage()      │───► [Hardware/Sim]      │
│   │  BLK_002: 8.0V   │    │   get_voltage()      │                         │
│   │  BLK_003: 0.0V   │    └──────────────────────┘                         │
│   └──────────────────┘                                                     │
│                                                                            │
│   Methods:                                                                 │
│   - update_for_train(train, target_speed) → set all occupied blocks       │
│   - set_block_voltage(block_id, voltage) → set specific block             │
│   - emergency_stop_all() → set all to 0V                                  │
└────────────────────────────────────────────────────────────────────────────┘
```

#### 8.2.2 Output Interface

```python
class OutputInterface(ABC):
    """Abstract interface for hardware voltage output."""
    
    @abstractmethod
    def set_voltage(self, block_id: str, voltage: float) -> bool:
        """Output voltage to a block. Returns success."""
        pass
    
    @abstractmethod
    def get_actual_voltage(self, block_id: str) -> float:
        """Read actual voltage (for feedback). Returns voltage."""
        pass

class SimulatedOutputInterface(OutputInterface):
    """Simulated output - just stores values for simulation."""
    
    def __init__(self):
        self._voltages: Dict[str, float] = {}
    
    def set_voltage(self, block_id: str, voltage: float) -> bool:
        self._voltages[block_id] = voltage
        return True
    
    def get_actual_voltage(self, block_id: str) -> float:
        return self._voltages.get(block_id, 0.0)
```

#### 8.2.3 BlockPowerController Class

```python
@dataclass
class BlockPowerController:
    """Manages DC voltage output to track blocks."""
    
    network: 'RailwayNetwork'
    output_interface: OutputInterface = field(default_factory=SimulatedOutputInterface)
    
    max_voltage: float = 12.0
    min_voltage: float = 0.0
    
    def update_for_train(self, train: Train, target_speed: float) -> None:
        """
        Update voltage for all blocks occupied by this train.
        
        Uses train's SpeedProfile to convert target_speed to voltage.
        """
        profile = self._get_speed_profile(train)
        voltage = profile.speed_to_voltage(target_speed)
        
        for block_id in train.occupied_blocks:
            self.set_block_voltage(block_id, voltage)
    
    def set_block_voltage(self, block_id: str, voltage: float) -> bool:
        """Set voltage for a specific block."""
        clamped = max(self.min_voltage, min(self.max_voltage, voltage))
        
        # Update block's stored voltage
        block = self.network.blocks.get(block_id)
        if block:
            block.current_voltage = clamped
        
        return self.output_interface.set_voltage(block_id, clamped)
    
    def get_block_voltage(self, block_id: str) -> float:
        """Get current voltage for a block."""
        return self.output_interface.get_actual_voltage(block_id)
    
    def emergency_stop_all(self) -> None:
        """Set all blocks to 0V immediately."""
        for block_id in self.network.blocks:
            self.set_block_voltage(block_id, 0.0)
    
    def release_block(self, block_id: str) -> None:
        """Set block to 0V (no train occupying)."""
        self.set_block_voltage(block_id, 0.0)
    
    def _get_speed_profile(self, train: Train) -> SpeedProfile:
        """Get speed profile for train."""
        if train.speed_profile_id:
            profile = self.network.speed_profiles.get(train.speed_profile_id)
            if profile:
                return profile
        return DEFAULT_SPEED_PROFILE
```

### 8.3 Block Class

Block is a simple data class - state management is done by BlockManager.

```python
@dataclass
class Block:
    """
    Track block - electrically isolated section of track.
    
    Note: State transitions are managed by BlockManager.
    Block only stores state, doesn't enforce transitions.
    """
    
    block_id: str
    name: str = ""
    length_cm: float = 100.0
    speed_limit_kmh: Optional[float] = None  # None = no limit
    
    # State (managed by BlockManager)
    state: BlockState = BlockState.FREE
    occupied_by: Optional[str] = None  # train_id
    reserved_by: Optional[str] = None  # path_id
    
    # Power (managed by BlockPowerController)
    current_voltage: float = 0.0
    
    # Track tiles in this block
    tiles: List[Tuple[int, int]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API/storage."""
        return {
            'block_id': self.block_id,
            'name': self.name,
            'length_cm': self.length_cm,
            'speed_limit_kmh': self.speed_limit_kmh,
            'state': self.state.value,
            'occupied_by': self.occupied_by,
            'reserved_by': self.reserved_by,
            'current_voltage': self.current_voltage,
            'tiles': [list(t) for t in self.tiles],
        }
```

---

## 9. SimulationController Integration

### 9.1 Purpose

SimulationController is the **orchestrator** that coordinates all train control and physics. It owns:
- Simulation time (global clock)
- TrainController instances (one per train)
- TrainPhysics instances (one per train)
- Route execution

### 9.2 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SimulationController                                 │
│                         (Orchestrator / Clock)                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Properties:                                                                │
│  ───────────                                                                │
│  • simulation_time: float          # Global clock (seconds)                 │
│  • is_running: bool                # Simulation paused/running              │
│  • tick_rate: float                # Ticks per second (default: 10)         │
│                                                                             │
│  Owned Instances:                                                           │
│  ─────────────────                                                          │
│  • controllers: Dict[str, TrainController]  # One per train                 │
│  • physics: Dict[str, TrainPhysics]         # One per train                 │
│  • routes: Dict[str, Route]                 # All defined routes            │
│                                                                             │
│  References:                                                                │
│  ───────────                                                                │
│  • network: RailwayNetwork                                                  │
│  • path_dispatcher: PathDispatcher                                          │
│  • power_controller: BlockPowerController                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 9.3 Main Update Loop

```python
def update(self, delta: float) -> None:
    """
    Main simulation tick - called by server loop.
    
    Args:
        delta: Time elapsed since last tick (seconds)
    """
    if not self.is_running:
        return
    
    # 1. Advance simulation time
    self.simulation_time += delta
    
    # 2. Update route execution (check for arrivals, departures)
    self._update_routes(delta)
    
    # 3. Update each train
    for train_id, train in self.network.trains.items():
        if not train.is_placed():
            continue
        
        controller = self.controllers.get(train_id)
        physics = self.physics.get(train_id)
        
        if not controller or not physics:
            continue
        
        # Step 1: Controller calculates ATC limits and ATO decisions
        controller.tick(delta)
        
        # Step 2: Physics executes movement
        physics.tick(delta)
```

### 9.4 Train Lifecycle Management

#### 9.4.1 Creating Controller and Physics for a Train

```python
def register_train(self, train_id: str) -> None:
    """
    Create controller and physics instances for a train.
    
    Called when a train is added to the network.
    """
    train = self.network.trains.get(train_id)
    if not train:
        return
    
    # Create controller
    controller = TrainController(
        train_id=train_id,
        network=self.network,
    )
    self.controllers[train_id] = controller
    
    # Create physics (references controller)
    physics = TrainPhysics(
        train_id=train_id,
        network=self.network,
        controller=controller,
    )
    self.physics[train_id] = physics

def unregister_train(self, train_id: str) -> None:
    """
    Remove controller and physics instances for a train.
    
    Called when a train is removed from the network.
    """
    self.controllers.pop(train_id, None)
    self.physics.pop(train_id, None)
```

#### 9.4.2 Placing a Train

```python
def place_train(
    self,
    train_id: str,
    tile_x: int,
    tile_y: int,
    facing_direction: Direction
) -> bool:
    """
    Place a train on the track at specified position.
    
    Creates TrainPosition with head and tail, occupies blocks.
    """
    train = self.network.trains.get(train_id)
    if not train:
        return False
    
    track = self.network.get_track(tile_x, tile_y)
    if not track:
        return False
    
    # Calculate entry edge from facing direction
    entry_edge = OPPOSITE_EDGES[facing_direction.name[0]]
    
    # Get tile length
    tile_length_cm = self._get_tile_length_cm(tile_x, tile_y)
    
    # Calculate occupied tiles and tail position
    (occupied_tiles, tail_tile, tail_distance, tail_entry) = \
        self._calculate_occupied_tiles(
            head_tile=(tile_x, tile_y),
            head_distance_cm=tile_length_cm,  # Head at exit edge
            head_entry_edge=entry_edge,
            train_length_cm=train.length_cm
        )
    
    # Create TrainPosition
    train.position = TrainPosition(
        end_a_tile=(tile_x, tile_y),
        end_a_distance_cm=tile_length_cm,
        end_a_entry_edge=entry_edge,
        end_b_tile=tail_tile,
        end_b_distance_cm=tail_distance,
        end_b_entry_edge=tail_entry,
        leading_end="a",
        facing_direction=facing_direction,
        occupied_tiles=occupied_tiles,
    )
    
    train.facing_direction = facing_direction
    
    # Occupy blocks
    occupied_blocks = self._get_blocks_for_tiles(occupied_tiles)
    for block_id in occupied_blocks:
        self.network.block_manager.occupy_block(block_id, train_id)
    train.occupied_blocks = occupied_blocks
    
    # Initialize controller state
    controller = self.controllers.get(train_id)
    if controller:
        controller.state = TrainState.OFF
    
    return True
```

### 9.5 Route Execution

#### 9.5.1 Route Update Logic

```python
def _update_routes(self, delta: float) -> None:
    """
    Update route execution for all trains with active routes.
    
    Checks for:
    - Train arrived at waypoint → start dwell
    - Dwell complete → dispatch to next waypoint
    - Route complete → deactivate
    """
    for train_id, train in self.network.trains.items():
        controller = self.controllers.get(train_id)
        if not controller:
            continue
        
        route = self._get_route_for_train(train_id)
        if not route or not route.is_active:
            continue
        
        waypoint = route.get_current_waypoint()
        if not waypoint:
            continue
        
        # Check if train arrived at waypoint station
        if controller.state == TrainState.RUNNING_MOVING:
            if self._is_at_station(train, waypoint.station_id):
                self._arrive_at_station(train_id, waypoint)
        
        # Check if dwell complete
        elif controller.state == TrainState.RUNNING_AT_STATION:
            if self._should_depart(train_id, waypoint, route):
                self._depart_from_station(train_id, route)

def _arrive_at_station(self, train_id: str, waypoint: Waypoint) -> None:
    """Handle train arrival at waypoint station."""
    controller = self.controllers[train_id]
    
    controller.state = TrainState.RUNNING_AT_STATION
    controller.target_speed = 0.0
    controller.handle_position = HandlePosition.N
    controller.ato_running = False
    
    # Record arrival time for dwell calculation
    self._station_arrival_times[train_id] = self.simulation_time

def _depart_from_station(self, train_id: str, route: Route) -> None:
    """Handle train departure from waypoint station."""
    controller = self.controllers[train_id]
    train = self.network.trains[train_id]
    
    # Advance to next waypoint
    next_waypoint = route.advance_to_next_waypoint()
    
    if next_waypoint is None:
        # Route complete
        route.is_active = False
        controller.state = TrainState.IDLE
        controller.active_cab = None
        return
    
    # Check if turn-around needed
    if self._needs_turnaround(train, next_waypoint.station_id):
        train.facing_direction = OPPOSITE_DIRECTIONS[train.facing_direction]
    
    # Dispatch to next station
    success = self._dispatch_to_station(train_id, next_waypoint.station_id)
    
    if success:
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True
```

### 9.6 Command Forwarding

SimulationController forwards commands to TrainController:

```python
# Power commands
def train_power_on(self, train_id: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.power_on_cmd()

def train_power_off(self, train_id: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.power_off_cmd()

def train_emergency_stop(self, train_id: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.emergency_stop_cmd()

# Cab/mode commands
def train_set_cab(self, train_id: str, cab: Optional[str]) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    result = controller.set_cab(cab)
    if result.success:
        # Mirror to train entity for frontend
        self.network.trains[train_id].active_cab = cab
    return result

def train_set_mode(self, train_id: str, mode: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.set_mode(mode)

# ATO commands
def train_ato_go(self, train_id: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.ato_go()

def train_ato_stop(self, train_id: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    return controller.ato_stop()

# Handle commands
def train_set_handle(self, train_id: str, position: str) -> CommandResult:
    controller = self.controllers.get(train_id)
    if not controller:
        return CommandResult(False, "Train not found")
    try:
        handle = HandlePosition(position)
    except ValueError:
        return CommandResult(False, f"Invalid handle position: {position}")
    return controller.set_handle(handle)
```

### 9.7 Full Class Definition

```python
@dataclass
class SimulationController:
    """
    Orchestrator for train simulation.
    
    Owns simulation time, TrainController/TrainPhysics instances,
    and coordinates route execution.
    """
    
    network: 'RailwayNetwork'
    path_dispatcher: Optional['PathDispatcher'] = None
    
    # Simulation state
    simulation_time: float = 0.0
    is_running: bool = False
    tick_rate: float = 10.0  # Ticks per second
    
    # Per-train instances (owned by SimulationController)
    controllers: Dict[str, TrainController] = field(default_factory=dict)
    physics: Dict[str, TrainPhysics] = field(default_factory=dict)
    
    # Routes
    routes: Dict[str, Route] = field(default_factory=dict)
    
    # Internal tracking
    _station_arrival_times: Dict[str, float] = field(default_factory=dict)
    
    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    
    def start(self) -> None:
        """Start simulation."""
        self.is_running = True
    
    def stop(self) -> None:
        """Stop simulation."""
        self.is_running = False
    
    def reset(self) -> None:
        """Reset simulation to initial state."""
        self.simulation_time = 0.0
        self._station_arrival_times.clear()
        for controller in self.controllers.values():
            controller.state = TrainState.OFF
            controller.power_on = False
    
    # ─────────────────────────────────────────────────────────────────
    # Main Loop
    # ─────────────────────────────────────────────────────────────────
    
    def update(self, delta: float) -> None:
        """Main simulation tick."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Train Management
    # ─────────────────────────────────────────────────────────────────
    
    def register_train(self, train_id: str) -> None:
        """Create controller/physics for train."""
        ...
    
    def unregister_train(self, train_id: str) -> None:
        """Remove controller/physics for train."""
        ...
    
    def place_train(self, train_id: str, x: int, y: int, direction: Direction) -> bool:
        """Place train on track."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Route Management
    # ─────────────────────────────────────────────────────────────────
    
    def assign_route(self, train_id: str, route_id: str) -> CommandResult:
        """Assign a route to a train."""
        ...
    
    def activate_route(self, train_id: str) -> CommandResult:
        """Activate the assigned route."""
        ...
    
    def deactivate_route(self, train_id: str) -> CommandResult:
        """Deactivate the current route."""
        ...
    
    # ─────────────────────────────────────────────────────────────────
    # Command Forwarding
    # ─────────────────────────────────────────────────────────────────
    
    def train_power_on(self, train_id: str) -> CommandResult: ...
    def train_power_off(self, train_id: str) -> CommandResult: ...
    def train_emergency_stop(self, train_id: str) -> CommandResult: ...
    def train_set_cab(self, train_id: str, cab: Optional[str]) -> CommandResult: ...
    def train_set_mode(self, train_id: str, mode: str) -> CommandResult: ...
    def train_ato_go(self, train_id: str) -> CommandResult: ...
    def train_ato_stop(self, train_id: str) -> CommandResult: ...
    def train_set_handle(self, train_id: str, position: str) -> CommandResult: ...
    
    # ─────────────────────────────────────────────────────────────────
    # Serialization
    # ─────────────────────────────────────────────────────────────────
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize simulation state."""
        return {
            'simulation_time': self.simulation_time,
            'is_running': self.is_running,
            'tick_rate': self.tick_rate,
            'controllers': {
                tid: ctrl.to_dict() 
                for tid, ctrl in self.controllers.items()
            },
            'routes': {
                rid: route.to_dict() 
                for rid, route in self.routes.items()
            },
        }
```

---

## 10. API Endpoints

### 10.1 Train Control Endpoints

All train control endpoints forward to TrainController via SimulationController.

#### 10.1.1 Power Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/power/on` | POST | Power on train |
| `/api/train/<train_id>/power/off` | POST | Power off train |
| `/api/train/<train_id>/emergency` | POST | Activate emergency stop |
| `/api/train/<train_id>/emergency/release` | POST | Release emergency stop |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/power/on
# Request: (no body required)

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "state": "idle"
}

# Response (failure):
{
    "success": false,
    "error": "Power already on"
}
```

#### 10.1.2 Cab Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/cab` | POST | Set active cab |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/cab
# Request:
{
    "cab": "A"  # "A", "B", or null
}

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "active_cab": "A",
    "facing_direction": "EAST",
    "state": "standby"
}

# Response (failure):
{
    "success": false,
    "error": "Cannot switch cab while moving"
}
```

#### 10.1.3 Mode Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/mode` | POST | Set control mode (ato/manual) |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/mode
# Request:
{
    "mode": "manual"  # "ato" or "manual"
}

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "control_mode": "manual",
    "state": "manual"
}
```

#### 10.1.4 Handle Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/handle` | POST | Set handle position |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/handle
# Request:
{
    "position": "P3"  # P5-P1, N, B1-B8, EB
}

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "handle_position": "P3"
}

# Response (failure):
{
    "success": false,
    "error": "Handle controlled by ATO while running"
}
```

#### 10.1.5 ATO Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/ato/go` | POST | Start ATO movement |
| `/api/train/<train_id>/ato/stop` | POST | Stop ATO movement |
| `/api/train/<train_id>/ato/destination` | POST | Set ATO destination |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/ato/go
# Request: (no body required)

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "ato_running": true,
    "state": "running_moving"
}

# POST /api/train/TRAIN_001/ato/destination
# Request:
{
    "station_id": "STATION_002"
}

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "ato_destination_id": "STATION_002"
}
```

### 10.2 Train State Endpoints

#### 10.2.1 Get Train State

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>` | GET | Get full train state |
| `/api/train/<train_id>/controller` | GET | Get controller state only |
| `/api/trains` | GET | Get all trains |

**Response:**

```python
# GET /api/train/TRAIN_001
{
    "train": {
        "train_id": "TRAIN_001",
        "name": "Express 1",
        "model": "BR 218",
        "length_cm": 120.0,
        "max_speed": 100.0,
        "current_speed": 45.5,
        "facing_direction": "EAST",
        "active_cab": "A",
        "position": {
            "end_a": {"tile": [5, 3], "distance_cm": 42.0, "entry_edge": "W"},
            "end_b": {"tile": [4, 3], "distance_cm": 22.0, "entry_edge": "W"},
            "leading_end": "a",
            "occupied_tiles": [[4, 3], [5, 3]]
        },
        "occupied_blocks": ["BLOCK_002", "BLOCK_003"]
    },
    "controller": {
        "power_on": true,
        "emergency_stop_active": false,
        "active_cab": "A",
        "control_mode": "ato",
        "state": "running_moving",
        "target_speed": 60.0,
        "handle_position": "P3",
        "ato_running": true,
        "ato_destination_id": "STATION_002",
        "clearance_ahead_cm": 450.0,
        "atc_speed_limit": 80.0
    }
}
```

### 10.3 Train Placement Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/train/<train_id>/place` | POST | Place train on track |
| `/api/train/<train_id>/remove` | POST | Remove train from track |

**Request/Response:**

```python
# POST /api/train/TRAIN_001/place
# Request:
{
    "x": 5,
    "y": 3,
    "facing_direction": "EAST"
}

# Response (success):
{
    "success": true,
    "train_id": "TRAIN_001",
    "position": {
        "end_a": {"tile": [5, 3], "distance_cm": 50.0, "entry_edge": "W"},
        "end_b": {"tile": [4, 3], "distance_cm": 30.0, "entry_edge": "W"},
        "occupied_tiles": [[4, 3], [5, 3]]
    },
    "occupied_blocks": ["BLOCK_002", "BLOCK_003"]
}
```

### 10.4 Route Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/routes` | GET | Get all routes |
| `/api/route/<route_id>` | GET | Get route details |
| `/api/route` | POST | Create new route |
| `/api/route/<route_id>` | PUT | Update route |
| `/api/route/<route_id>` | DELETE | Delete route |
| `/api/train/<train_id>/route` | POST | Assign route to train |
| `/api/train/<train_id>/route/activate` | POST | Activate assigned route |
| `/api/train/<train_id>/route/deactivate` | POST | Deactivate route |

**Request/Response:**

```python
# POST /api/route
# Request:
{
    "name": "Downtown Loop",
    "mode": "loop",
    "waypoints": [
        {"station_id": "STATION_001", "dwell_time_sec": 30},
        {"station_id": "STATION_002", "dwell_time_sec": 45},
        {"station_id": "STATION_003", "dwell_time_sec": 30}
    ]
}

# Response:
{
    "success": true,
    "route_id": "ROUTE_001",
    "route": { ... }
}

# POST /api/train/TRAIN_001/route
# Request:
{
    "route_id": "ROUTE_001"
}

# Response:
{
    "success": true,
    "train_id": "TRAIN_001",
    "assigned_route_id": "ROUTE_001"
}
```

### 10.5 Simulation Control Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/simulation/start` | POST | Start simulation |
| `/api/simulation/stop` | POST | Stop simulation |
| `/api/simulation/reset` | POST | Reset simulation |
| `/api/simulation/status` | GET | Get simulation status |

**Response:**

```python
# GET /api/simulation/status
{
    "is_running": true,
    "simulation_time": 1234.5,
    "tick_rate": 10.0,
    "train_count": 3,
    "active_routes": 2
}
```

### 10.6 Block & Power Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/blocks` | GET | Get all blocks with states |
| `/api/block/<block_id>` | GET | Get block details |
| `/api/block/<block_id>/voltage` | GET | Get block voltage |
| `/api/power/emergency` | POST | Emergency stop all blocks |

**Response:**

```python
# GET /api/blocks
{
    "blocks": [
        {
            "block_id": "BLOCK_001",
            "name": "Main Line 1",
            "state": "free",
            "occupied_by": null,
            "current_voltage": 0.0,
            "length_cm": 150.0
        },
        {
            "block_id": "BLOCK_002",
            "name": "Main Line 2",
            "state": "occupied",
            "occupied_by": "TRAIN_001",
            "current_voltage": 8.0,
            "length_cm": 120.0
        }
    ]
}
```

### 10.7 Speed Profile Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/speed-profiles` | GET | Get all speed profiles |
| `/api/speed-profile/<profile_id>` | GET | Get profile details |
| `/api/speed-profile` | POST | Create new profile |
| `/api/speed-profile/<profile_id>` | PUT | Update profile |

---

## 11. State Machine Diagrams

### 11.1 TrainState State Machine

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          TRAIN STATE MACHINE                                 │
└─────────────────────────────────────────────────────────────────────────────┘

                              power_on()
    ┌───────────┐         ──────────────►         ┌───────────┐
    │           │                                 │           │
    │    OFF    │                                 │   IDLE    │
    │           │         ◄──────────────         │           │
    └───────────┘           power_off()           └─────┬─────┘
                          (must be stopped)             │
                                                        │
                                           set_cab('A'/'B')
                                                        │
                                                        ▼
                         set_mode('manual')       ┌───────────┐
               ┌─────────────────────────────────►│           │
               │         (must be stopped)        │  STANDBY  │
               │                                  │           │
               │         ◄─────────────────────── └─────┬─────┘
               │         set_mode('ato')                │
               │         (must be stopped)              │
    ┌──────────┴──────────┐                       ato_go()
    │                     │                             │
    │       MANUAL        │                             ▼
    │                     │                  ┌─────────────────────┐
    │  User controls      │                  │                     │
    │  handle position    │                  │   RUNNING_MOVING    │
    │  ATC still protects │                  │                     │
    └─────────────────────┘                  └──────────┬──────────┘
                                                        │
                          ┌─────────────────────────────┼─────────────────────┐
                          │                             │                     │
                          ▼                             ▼                     ▼
               ┌─────────────────────┐      ┌─────────────────────┐   path blocked/
               │                     │      │                     │   ato_stop()
               │ RUNNING_AT_STATION  │      │   RUNNING_WAITING   │          │
               │                     │      │                     │          │
               │  Dwelling at        │      │  Waiting for path/  │          │
               │  station            │      │  signal             │          │
               └──────────┬──────────┘      └──────────┬──────────┘          │
                          │                            │                     │
                          │ dwell complete             │ path clear          │
                          │                            │                     │
                          ▼                            ▼                     │
                   ┌─────────────────────────────────────────┐               │
                   │          Back to RUNNING_MOVING         │◄──────────────┘
                   │             or STANDBY                  │
                   └─────────────────────────────────────────┘


    State Descriptions:
    ────────────────────
    OFF                  : No power, 0V to blocks
    IDLE                 : Powered, no cab selected, lighting voltage
    STANDBY              : Cab selected, ready to depart, lighting voltage
    RUNNING_MOVING       : Actively moving, speed-based voltage
    RUNNING_WAITING      : Stopped waiting for path/signal, lighting voltage
    RUNNING_AT_STATION   : Dwelling at station, lighting voltage
    MANUAL               : User controls handle, ATC protects, speed-based voltage
```

### 11.2 Block State Machine

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          BLOCK STATE MACHINE                                 │
└─────────────────────────────────────────────────────────────────────────────┘

                         reserve_block()
    ┌───────────┐       (PathDispatcher)        ┌───────────┐
    │           │ ─────────────────────────────►│           │
    │   FREE    │                               │ RESERVED  │
    │           │◄───────────────────────────── │           │
    └───────────┘       release_block()         └─────┬─────┘
         ▲           (path cancelled before           │
         │            train entered)                  │
         │                                            │ occupy_block()
         │                                            │ (train head enters)
         │                                            ▼
         │                                      ┌───────────┐
         │                                      │           │
         │                                      │ OCCUPIED  │
         │                                      │           │
         │                                      └─────┬─────┘
         │                                            │
         │   clear_block()                            │ set_block_to_caution()
         │   (train fully exits)                      │ (train tail exiting)
         │                                            ▼
         │                                      ┌───────────┐
         │                                      │           │
         └──────────────────────────────────────│  CAUTION  │
                     clear_block()              │           │
                                                └───────────┘


    Valid Transitions:
    ───────────────────
    FREE      → RESERVED  : Path reservation
    FREE      → OCCUPIED  : Direct occupancy (no path)
    RESERVED  → FREE      : Path cancelled
    RESERVED  → OCCUPIED  : Train enters reserved block
    OCCUPIED  → CAUTION   : Train tail exiting
    CAUTION   → FREE      : Train fully exited
    OCCUPIED  → FREE      : Direct clear (short trains)
```

### 11.3 ATO Command Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ATO COMMAND FLOW                                   │
└─────────────────────────────────────────────────────────────────────────────┘

    User clicks "Go" button
            │
            ▼
    ┌───────────────────┐
    │ API: POST         │
    │ /train/.../ato/go │
    └─────────┬─────────┘
              │
              ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │                     SimulationController                               │
    │                     train_ato_go(train_id)                             │
    └─────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │                      TrainController.ato_go()                          │
    │                                                                        │
    │  Validate:                                                             │
    │  ✓ control_mode == 'ato'                                               │
    │  ✓ active_cab is set                                                   │
    │  ✓ state == STANDBY                                                    │
    │                                                                        │
    │  Execute:                                                              │
    │  → state = RUNNING_MOVING                                              │
    │  → ato_running = True                                                  │
    │  → Request path if destination set                                     │
    └─────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │                    Simulation Tick Loop                                │
    │                                                                        │
    │  TrainController.tick():                                               │
    │  → Calculate clearance ahead (ATC)                                     │
    │  → Calculate atc_speed_limit (braking curve)                           │
    │  → ATO selects handle_position to reach target                         │
    │                                                                        │
    │  TrainPhysics.tick():                                                  │
    │  → Read handle_position                                                │
    │  → Calculate acceleration                                              │
    │  → Update train.current_speed                                          │
    │  → Move train.position                                                 │
    │  → Update block occupancy                                              │
    │  → Output voltage to blocks                                            │
    └───────────────────────────────────────────────────────────────────────┘
```

### 11.4 Handle Position Effect

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       HANDLE POSITION → SPEED CHANGE                        │
└─────────────────────────────────────────────────────────────────────────────┘

    Handle Position          Acceleration              Speed Effect
    ───────────────          ────────────              ────────────

        P5  ──────────►  +100% accel_rate  ──────────►  Speed ↑↑↑
        P4  ──────────►   +80% accel_rate  ──────────►  Speed ↑↑
        P3  ──────────►   +60% accel_rate  ──────────►  Speed ↑
        P2  ──────────►   +40% accel_rate  ──────────►  Speed ↑
        P1  ──────────►   +20% accel_rate  ──────────►  Speed ↑

        N   ──────────►        0           ──────────►  Coast (no change)

        B1  ──────────►  -12.5% decel_rate ──────────►  Speed ↓
        B2  ──────────►   -25% decel_rate  ──────────►  Speed ↓
        B3  ──────────►  -37.5% decel_rate ──────────►  Speed ↓
        B4  ──────────►   -50% decel_rate  ──────────►  Speed ↓↓
        B5  ──────────►  -62.5% decel_rate ──────────►  Speed ↓↓
        B6  ──────────►   -75% decel_rate  ──────────►  Speed ↓↓
        B7  ──────────►  -87.5% decel_rate ──────────►  Speed ↓↓↓
        B8  ──────────►  -100% decel_rate  ──────────►  Speed ↓↓↓

        EB  ──────────►  -100% emergency   ──────────►  STOP NOW
                              _rate


    Example (SpeedProfile with accel=10, decel=15, emergency=30 km/h/s):
    ─────────────────────────────────────────────────────────────────────

    P5 at 50 km/h → +10 km/h/s → after 1s: 60 km/h
    N  at 50 km/h →   0 km/h/s → after 1s: 50 km/h (coast)
    B4 at 50 km/h → -7.5 km/h/s → after 1s: 42.5 km/h
    EB at 50 km/h → -30 km/h/s → after 1s: 20 km/h
```

---

## 12. Example Scenarios

### 12.1 Scenario: Power On and Depart (ATO Mode)

**Goal:** Power on a train, select cab, and depart in ATO mode.

```
Step 1: Power On
─────────────────
POST /api/train/TRAIN_001/power/on
→ state: OFF → IDLE
→ power_on: true
→ handle_position: N

Step 2: Select Cab A
─────────────────────
POST /api/train/TRAIN_001/cab  {"cab": "A"}
→ state: IDLE → STANDBY
→ active_cab: A
→ facing_direction: (unchanged)

Step 3: Set Destination
────────────────────────
POST /api/train/TRAIN_001/ato/destination  {"station_id": "STATION_002"}
→ ato_destination_id: STATION_002

Step 4: Depart (ATO Go)
────────────────────────
POST /api/train/TRAIN_001/ato/go
→ state: STANDBY → RUNNING_MOVING
→ ato_running: true
→ Path requested to STATION_002

Step 5: Train Moving (Simulation Ticks)
────────────────────────────────────────
Tick 1:
  TrainController.tick():
    → clearance_ahead_cm: 500
    → atc_speed_limit: 80 km/h
    → ATO selects handle: P4 (accelerate)
  TrainPhysics.tick():
    → acceleration: +8 km/h/s
    → current_speed: 0 → 0.8 km/h
    → position updated
    → voltage: 3V

Tick 10 (1 second later):
  → current_speed: 8 km/h
  → voltage: 3.5V

Tick 100 (10 seconds later):
  → current_speed: 60 km/h
  → ATO selects handle: N (coast, at target)
  → voltage: 8V
```

### 12.2 Scenario: Emergency Stop

**Goal:** Activate emergency stop while train is moving.

```
Initial State:
──────────────
→ state: RUNNING_MOVING
→ current_speed: 60 km/h
→ ato_running: true

Step 1: Emergency Stop
───────────────────────
POST /api/train/TRAIN_001/emergency
→ emergency_stop_active: true
→ handle_position: EB
→ ato_running: false

Step 2: Train Braking (Simulation Ticks)
─────────────────────────────────────────
Tick 1:
  TrainController.tick():
    → handle_position: EB (forced)
    → target_speed: 0
  TrainPhysics.tick():
    → acceleration: -30 km/h/s (emergency)
    → current_speed: 60 → 57 km/h
    → voltage: 0V (emergency cuts power)

Tick 20 (2 seconds later):
  → current_speed: 0 km/h
  → state: RUNNING_WAITING (or stays RUNNING_MOVING until fully stopped)

Step 3: Release Emergency
──────────────────────────
POST /api/train/TRAIN_001/emergency/release
(Must be stopped, handle must be in N or B)
→ emergency_stop_active: false
→ handle_position: N
→ state: STANDBY (ready to depart again)
```

### 12.3 Scenario: Manual Mode Driving

**Goal:** Switch to manual mode and control train with handle.

```
Initial State:
──────────────
→ state: STANDBY
→ control_mode: ato
→ current_speed: 0

Step 1: Switch to Manual Mode
──────────────────────────────
POST /api/train/TRAIN_001/mode  {"mode": "manual"}
→ state: STANDBY → MANUAL
→ control_mode: manual

Step 2: Apply Power (P3)
─────────────────────────
POST /api/train/TRAIN_001/handle  {"position": "P3"}
→ handle_position: P3
→ Train starts accelerating

Step 3: Coast (N)
──────────────────
POST /api/train/TRAIN_001/handle  {"position": "N"}
→ handle_position: N
→ Train coasts at current speed

Step 4: Brake (B4)
───────────────────
POST /api/train/TRAIN_001/handle  {"position": "B4"}
→ handle_position: B4
→ Train decelerates

Step 5: ATC Override (if approaching obstacle)
───────────────────────────────────────────────
TrainController.tick():
  → clearance_ahead_cm: 100
  → atc_speed_limit: 20 km/h
  → If current_speed > atc_limit:
      ATC forces braking (overrides user handle)
```

### 12.4 Scenario: Turn-Around at Terminal Station

**Goal:** Train arrives at terminal, turns around, departs opposite direction.

```
Initial State:
──────────────
→ state: RUNNING_MOVING
→ facing_direction: EAST
→ active_cab: A
→ Approaching STATION_003 (terminal)

Step 1: Arrival at Station
───────────────────────────
SimulationController detects arrival
→ state: RUNNING_MOVING → RUNNING_AT_STATION
→ ato_running: false
→ current_speed: 0
→ Dwell timer starts

Step 2: Dwell Complete, Next Waypoint Requires Turn-Around
───────────────────────────────────────────────────────────
Route.advance_to_next_waypoint():
→ Next waypoint: STATION_002 (behind current position)

SimulationController._depart_from_station():
→ Detects turn-around needed
→ facing_direction: EAST → WEST (flipped!)
→ active_cab stays "A" (but now facing other way)

Step 3: Dispatch to Next Station
─────────────────────────────────
→ Request path to STATION_002
→ state: RUNNING_AT_STATION → RUNNING_MOVING
→ ato_running: true
→ Train now moves WEST
```

### 12.5 Scenario: Path Blocked (Error Handling)

**Goal:** Train cannot find path to destination, handles error gracefully.

```
Initial State:
──────────────
→ state: RUNNING_AT_STATION
→ Dwell complete at STATION_001
→ Next waypoint: STATION_002

Step 1: Attempt to Dispatch
────────────────────────────
SimulationController._dispatch_to_station():
→ PathDispatcher.create_path() fails
→ Error: "Path blocked by TRAIN_002"

Step 2: Error Handling
───────────────────────
→ state: RUNNING_AT_STATION → RUNNING_WAITING
→ target_speed: 0
→ route.last_error: "No path to STATION_002: Path blocked by TRAIN_002"
→ route.error_time: 1234.5

Step 3: UI Displays Error
──────────────────────────
GET /api/train/TRAIN_001:
{
    "controller": {
        "state": "running_waiting",
        "ato_running": false
    },
    "route": {
        "last_error": "No path to STATION_002: Path blocked by TRAIN_002",
        "error_time": 1234.5
    }
}

Step 4: Path Becomes Available (Later)
───────────────────────────────────────
Operator clears TRAIN_002, retries:
POST /api/train/TRAIN_001/ato/go
→ Path request succeeds
→ state: RUNNING_WAITING → RUNNING_MOVING
→ route.last_error: null
```

### 12.6 Scenario: Multi-Block Train Occupancy

**Goal:** Long train occupies multiple blocks as it moves.

```
Setup:
──────
Train length: 120 cm
Block lengths: BLOCK_001 (50cm), BLOCK_002 (50cm), BLOCK_003 (50cm)

Initial Position:
─────────────────
Head at: BLOCK_002 (50cm into block)
Tail at: BLOCK_001 (30cm into block)
→ occupied_blocks: [BLOCK_001, BLOCK_002]
→ BLOCK_001.state: OCCUPIED
→ BLOCK_002.state: OCCUPIED

Step 1: Train Moves Forward (50cm)
───────────────────────────────────
TrainPhysics._move_train():
→ Head moves into BLOCK_003
→ TrainPhysics._update_block_occupancy():
    → New block: BLOCK_003 → occupy_block(BLOCK_003, TRAIN_001)
    → BLOCK_003.state: FREE → OCCUPIED

→ occupied_blocks: [BLOCK_001, BLOCK_002, BLOCK_003]

Step 2: Train Continues (50cm more)
────────────────────────────────────
→ Tail exits BLOCK_001
→ TrainPhysics._update_block_occupancy():
    → Released block: BLOCK_001 → clear_block(BLOCK_001)
    → BLOCK_001.state: OCCUPIED → FREE

→ occupied_blocks: [BLOCK_002, BLOCK_003]

Voltage During Movement:
────────────────────────
All occupied blocks receive same voltage:
→ BLOCK_002.voltage: 8V
→ BLOCK_003.voltage: 8V
→ BLOCK_001.voltage: 0V (released)
```

---

## Appendix A: Configuration Constants

```python
# Speed thresholds
STOPPED_SPEED_THRESHOLD_KMH = 0.1  # Below this = stopped
SPEED_TOLERANCE_KMH = 0.5          # For ATO "at target" detection

# ATC Safety
ATC_SAFETY_MARGIN_CM = 50.0        # Buffer before obstruction
YELLOW_SIGNAL_SPEED_KMH = 30.0     # Speed limit on yellow signal

# Default timing
DEFAULT_DWELL_TIME_SEC = 30.0      # Default station stop duration
DEFAULT_TICK_RATE = 10.0           # Ticks per second

# Handle model
HANDLE_ORDER = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 
                'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB']

# Direction mapping
OPPOSITE_DIRECTIONS = {
    Direction.NORTH: Direction.SOUTH,
    Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.WEST,
    Direction.WEST: Direction.EAST,
}

OPPOSITE_EDGES = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}
```

---

## Appendix B: File Summary

| File | Purpose | Status |
|------|---------|--------|
| `train_controller.py` | Combined ATC + ATO + command handling | Active |
| `train_physics.py` | Physical movement, block/voltage management | Active |
| `train.py` | Train data entity | Active |
| `speed_profile.py` | Speed-voltage mapping, dynamics | Active |
| `train_route.py` | Route and Waypoint classes | Active |
| `block_manager.py` | Block state management | Active |
| `block_power.py` | Voltage output control | Active |
| `simulation.py` | SimulationController | Active |

---

*End of Document*
