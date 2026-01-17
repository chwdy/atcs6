# Technical Specification: TrainMover (Onboard Computer)

> **Document Type:** Technical Implementation Specification  
> **Version:** 1.1  
> **Created:** 2026-01-16  
> **Status:** Design Complete

## Table of Contents

1. [Overview & Purpose](#1-overview--purpose)
2. [Architecture & Entity Relationships](#2-architecture--entity-relationships)
3. [Scope & Responsibilities](#3-scope--responsibilities)
4. [Throttle & Brake Handle Model](#4-throttle--brake-handle-model)
5. [Control Flow](#5-control-flow)
6. [State Flow](#6-state-flow)
7. [TrainMover Specification](#7-trainmover-specification)
8. [UI Integration](#8-ui-integration)
9. [API Endpoints](#9-api-endpoints)
10. [Implementation Notes](#10-implementation-notes)

---

## 1. Overview & Purpose

### 1.1 What is TrainMover?

TrainMover is the **onboard computer** for each train. It acts as the single authority for train operational state and control decisions. Think of it like the cab computer in a real train that:

- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Knows which cab is active and the travel direction
- Manages the throttle/brake handle position
- Decides whether buttons/commands should work
- Stores and manages destination/path/schedule information
- Reports current operational state to displays

### 1.2 Design Goals

| Goal | Description |
|------|-------------|
| **Single Authority** | TrainMover owns all train control state (cab, direction, mode, ATO status) |
| **Command Pattern** | UI sends commands to TrainMover; TrainMover validates and executes |
| **Read-Only UI** | Control panel displays read from TrainMover state, never compute locally |
| **Separation of Concerns** | TrainMover handles control logic; SimulationController handles time/physics |

### 1.3 Key Design Decision

**Problem:** The train control panel had logic issues:
- Speed display not updating
- Cab switch allowed while moving
- Direction indicators not updating on cab switch
- ATO "Go" not respecting cab switch for turnback

**Solution:** TrainMover becomes the single source of truth for:
- Power state (on/off) and emergency stop
- Active cab (A/B/None)
- Throttle/brake handle position (P1-P5, N, B1-B8, EB)
- Travel direction (forward/reverse relative to active cab)
- Control mode (ATO/Manual)
- ATO destination, path, and schedule
- Whether commands are allowed (validation)

---

## 2. Architecture & Entity Relationships

### 2.1 System Architecture Diagram

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
│  │  • Calls update(delta) on tick                                        │   │
│  │  • Iterates all trains, delegates to TrainMover                       │   │
│  │  • Coordinates BlockPowerController for voltage                       │   │
│  └───────────────────────────────┬──────────────────────────────────────┘   │
│                                  │                                          │
│                                  │ update_train(train, delta)               │
│                                  ▼                                          │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                        TrainMover                                     │   │
│  │                    (Onboard Computer)                                 │   │
│  │  ─────────────────────────────────────────────────────────────────   │   │
│  │  Per-Train Instance:                                                  │   │
│  │  • power_on: bool                                                     │   │
│  │  • emergency_stop: bool                                               │   │
│  │  • active_cab: 'A' | 'B' | None                                      │   │
│  │  • control_mode: 'ato' | 'manual'                                    │   │
│  │  • handle_position: 'P5'..'P1' | 'N' | 'B1'..'B8' | 'EB'            │   │
│  │  • travel_direction: 'forward' | 'reverse'                           │   │
│  │  • ato_destination_id: Optional[str]                                 │   │
│  │  • ato_path_id: Optional[str]                                        │   │
│  │  • ato_schedule_id: Optional[str]                                    │   │
│  │                                                                       │   │
│  │  Commands:                                                            │   │
│  │  • power_on() / power_off() → power control                          │   │
│  │  • emergency_stop() → immediate stop                                 │   │
│  │  • set_cab(cab) → validates train stopped                            │   │
│  │  • set_handle(position) → throttle/brake control                     │   │
│  │  • set_mode(mode) → validates preconditions                          │   │
│  │  • ato_go() → starts movement                                        │   │
│  │  • ato_stop() → stops movement                                       │   │
│  └───────────────────────────────┬──────────────────────────────────────┘   │
│                                  │                                          │
│              ┌───────────────────┼───────────────────┐                      │
│              │                   │                   │                      │
│              ▼                   ▼                   ▼                      │
│  ┌───────────────────┐  ┌───────────────┐  ┌─────────────────────┐         │
│  │      Train        │  │  BlockManager │  │     TrainATC        │         │
│  │   (Data Entity)   │  │(Block State)  │  │  (Speed Limiting)   │         │
│  │  ───────────────  │  │  ───────────  │  │  ─────────────────  │         │
│  │  • position       │  │  • occupy()   │  │  • get_clearance()  │         │
│  │  • current_speed  │  │  • release()  │  │  • get_speed_limit()│         │
│  │  • facing_dir     │  │  • state      │  │  • braking curves   │         │
│  │  • length_cm      │  │               │  │                     │         │
│  └───────────────────┘  └───────────────┘  └─────────────────────┘         │
│                                  │                                          │
│                                  ▼                                          │
│                         ┌───────────────┐                                   │
│                         │     Block     │                                   │
│                         │  (Data Only)  │                                   │
│                         └───────────────┘                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                             FRONTEND (JavaScript)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │                      StateManager                                   │     │
│  │  ────────────────────────────────────────────────────────────────  │     │
│  │  • Polls backend for network state                                  │     │
│  │  • Provides subscription API for UI components                      │     │
│  │  • Single source for all UI reads                                   │     │
│  └──────────────────────────────┬─────────────────────────────────────┘     │
│                                 │                                           │
│            ┌────────────────────┼────────────────────┐                      │
│            │                    │                    │                      │
│            ▼                    ▼                    ▼                      │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐            │
│  │ Train Info Panel │ │Train Control Panel│ │ Train Debug Panel│            │
│  │  (Read-Only)     │ │  (Commands)       │ │   (Read-Only)    │            │
│  │  ──────────────  │ │  ──────────────── │ │  ──────────────  │            │
│  │  • Position      │ │  • Power On/Off   │ │  • Path tiles    │            │
│  │  • Speed display │ │  • Emergency Stop │ │  • Block states  │            │
│  │  • Direction     │ │  • Cab A/B/Off    │ │  • ATC info      │            │
│  │  • State         │ │  • ATO/Manual     │ │  • Lookahead     │            │
│  │                  │ │  • Go/Stop        │ │                  │            │
│  │                  │ │  • Handle P/N/B   │ │                  │            │
│  │  Source: Train   │ │  Source: TrainMover│ │  Source: Train  │            │
│  └──────────────────┘ └────────┬─────────┘ └──────────────────┘            │
│                                │                                            │
│                                │ Button Click                               │
│                                ▼                                            │
│                       ┌────────────────┐                                    │
│                       │    API.js      │                                    │
│                       │  ────────────  │                                    │
│                       │  trainSetCab() │───────► POST /api/train/.../cab    │
│                       │  trainDepart() │───────► POST /api/train/.../depart │
│                       │  trainStop()   │───────► POST /api/train/.../stop   │
│                       └────────────────┘                                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Entity Relationship Summary

```
Train ←───────── TrainMover ←────────→ BlockManager ───────→ Block
  │                  │                        │
  │                  │                        │
  │                  ▼                        │
  │              TrainATC                     │
  │                  │                        │
  │                  ▼                        │
  └──────────→ SpeedProfile                   │
                     │                        │
                     ▼                        ▼
              BlockPowerController ─────→ [Voltage Output]
```

| Relationship | Description |
|--------------|-------------|
| Train ← TrainMover | TrainMover reads/writes Train position, speed, direction |
| TrainMover ↔ BlockManager | TrainMover requests block occupy/release; BlockManager validates |
| TrainMover → TrainATC | TrainMover queries ATC for speed limits |
| BlockManager → Block | BlockManager is sole authority for Block state changes |

---

## 3. Scope & Responsibilities

### 3.1 TrainMover Owns (Onboard Computer State)

| Property | Type | Description |
|----------|------|-------------|
| `power_on` | `bool` | Whether train power is on |
| `emergency_stop_active` | `bool` | Emergency stop engaged |
| `active_cab` | `'A'` \| `'B'` \| `None` | Which cab is currently active |
| `control_mode` | `'ato'` \| `'manual'` | Current control mode |
| `handle_position` | `str` | Throttle/brake handle position (see §4) |
| `travel_direction` | `'forward'` \| `'reverse'` | Direction relative to active cab |
| `ato_destination_id` | `Optional[str]` | Target station/block for ATO |
| `ato_path_id` | `Optional[str]` | Assigned path ID from dispatcher |
| `ato_schedule_id` | `Optional[str]` | Schedule being followed (future) |
| `ato_running` | `bool` | Whether ATO is actively running |

### 3.2 TrainMover Validates (Command Gating)

| Command | Validation Rule |
|---------|-----------------|
| `power_on()` | Train must be in OFF state |
| `power_off()` | Train must be stopped |
| `emergency_stop()` | Always allowed (safety override) |
| `release_emergency()` | Train must be stopped, handle in N or B |
| `set_cab(cab)` | Train must be stopped (speed = 0) |
| `set_mode('manual')` | Train must be stopped |
| `set_mode('ato')` | Train must be stopped |
| `set_handle(position)` | Valid position, respects ATC limits |
| `ato_go()` | Active cab must be set, train in standby |
| `ato_stop()` | Only when ATO is running |
| `set_travel_direction()` | Train must be stopped, only in manual mode |

### 3.3 SimulationController Owns (World State)

| Responsibility | Description |
|----------------|-------------|
| `simulation_time` | Global clock for the simulation |
| `update(delta)` | Tick function called by server loop |
| Iteration order | Decides order of train updates |
| Physics integration | Applies speed profile, acceleration |
| Position updates | Moves trains based on speed and delta |

### 3.4 Train Entity Owns (Physical State)

| Property | Description |
|----------|-------------|
| `position` | TrainPosition with head/tail tracking |
| `current_speed` | Actual speed (from physics) |
| `facing_direction` | Direction train is facing (flips on cab switch) |
| `length_cm` | Physical length of train |
| `max_speed` | Maximum allowed speed |
| `occupied_blocks` | List of blocks train occupies |

### 3.5 BlockManager Owns (Block Occupancy)

| Responsibility | Description |
|----------------|-------------|
| State machine | FREE → RESERVED → OCCUPIED → CAUTION → FREE |
| Atomic operations | `occupy_block()`, `release_block()`, `clear_block()` |
| Conflict detection | Prevents double-occupancy |

---

## 4. Throttle & Brake Handle Model

### 4.1 Overview

Real trains are not driven by setting a target speed directly. Instead, the driver uses a **combined throttle/brake handle** with discrete notches:

- **Power notches (P1-P5):** Apply acceleration
- **Neutral (N):** Coast (no power, no braking)
- **Brake notches (B1-B8):** Apply service braking
- **Emergency Brake (EB):** Maximum braking force

The handle position determines acceleration/deceleration rate, and the train's speed changes over time based on the SpeedProfile's rates.

### 4.2 Handle Positions

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

### 4.3 Handle Position Enum

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

### 4.4 Speed Calculation from Handle

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

### 4.5 ATO Handle Control

When in ATO mode, the onboard computer automatically controls the handle:

```python
def ato_calculate_handle(
    train_mover: 'TrainMover',
    current_speed: float,
    target_speed: float,  # From route/schedule
    atc_limit: float,
    distance_to_stop: float  # Distance to next stop
) -> HandlePosition:
    """
    ATO automatically selects appropriate handle position.
    
    The ATO system:
    1. Respects ATC limits (never exceeds)
    2. Follows scheduled speeds
    3. Brakes smoothly for stops
    """
    effective_target = min(target_speed, atc_limit)
    
    # Emergency stop override
    if train_mover.emergency_stop_active:
        return HandlePosition.EB
    
    # Need to stop
    if effective_target == 0 or distance_to_stop < STOP_DISTANCE_THRESHOLD:
        # Calculate required braking
        if current_speed > 0:
            return _calculate_braking_notch(current_speed, distance_to_stop)
        return HandlePosition.N
    
    # At target speed - coast
    if abs(current_speed - effective_target) < SPEED_TOLERANCE:
        return HandlePosition.N
    
    # Below target - accelerate
    if current_speed < effective_target:
        return _calculate_power_notch(current_speed, effective_target)
    
    # Above target - brake
    return _calculate_braking_notch_for_speed(current_speed, effective_target)
```

### 4.6 Debug API (Target Speed)

For debugging and testing, a direct target speed API is available:

```python
def set_target_speed_debug(self, speed: float) -> CommandResult:
    """
    DEBUG ONLY: Set target speed directly.
    
    This bypasses the handle model and sets a target speed.
    The simulation will automatically apply appropriate
    acceleration/deceleration to reach this speed.
    
    Warning: This is for debugging only. Production code
    should use set_handle() instead.
    """
    if not self._is_debug_mode():
        return CommandResult(False, "Debug mode not enabled")
    
    train = self._get_train()
    train.target_speed = max(0, min(speed, train.max_speed))
    
    return CommandResult(True, data={"target_speed": train.target_speed})
```

---

## 5. Control Flow

### 5.1 Simulation Tick Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Server Simulation Loop                            │
│                        (server.py:simulation_loop)                       │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    │ Every 100ms
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     SimulationController.update(delta)                   │
│                                                                         │
│  1. simulation_time += delta                                            │
│  2. route_executor.update(delta)                                        │
│  3. FOR each train in network.trains:                                   │
│         _update_train(train, delta)                                     │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     _update_train(train, delta)                          │
│                                                                         │
│  1. Get TrainMover for this train                                       │
│  2. Check train.state (OFF → skip, IDLE → decelerate, etc.)            │
│  3. If RUNNING_MOVING:                                                  │
│     a. Query TrainATC.get_speed_limit()                                 │
│     b. Calculate target_speed = min(max_speed, atc_limit)               │
│     c. Apply SpeedProfile acceleration/deceleration                     │
│     d. Call TrainMover.update_position(train, delta)                    │
│     e. Update block occupancy via BlockManager                          │
│  4. Update BlockPowerController voltage                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Command Flow (UI → Backend)

```
┌──────────────┐   Click    ┌───────────────┐   POST    ┌─────────────────┐
│  UI Button   │ ─────────► │   api.js      │ ────────► │  Flask Endpoint │
│  (Cab A)     │            │ trainSetCab() │           │ /train/.../cab  │
└──────────────┘            └───────────────┘           └────────┬────────┘
                                                                 │
                                                                 ▼
                                                    ┌────────────────────────┐
                                                    │  TrainMover.set_cab()  │
                                                    │                        │
                                                    │  1. Validate stopped   │
                                                    │  2. Update active_cab  │
                                                    │  3. Flip facing_dir    │
                                                    │  4. Return success     │
                                                    └────────────────────────┘
                                                                 │
                                                                 ▼
                                                    ┌────────────────────────┐
                                                    │  Response to UI        │
                                                    │  { success: true }     │
                                                    └────────────────────────┘
                                                                 │
                                                                 ▼
                                                    ┌────────────────────────┐
                                                    │  UI Refreshes Panel    │
                                                    │  (polls StateManager)  │
                                                    └────────────────────────┘
```

---

## 6. State Flow

### 6.1 Cab Switch State Machine

```
                              Train Stopped (speed = 0)
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
                    ▼                   ▼                   ▼
            ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
            │   Cab A     │     │   Cab B     │     │   Cab Off   │
            │  (Active)   │     │  (Active)   │     │  (Inactive) │
            │             │     │             │     │             │
            │ facing_dir  │     │ facing_dir  │     │ facing_dir  │
            │ = original  │     │ = flipped   │     │ = last      │
            │             │     │             │     │             │
            │ state:      │     │ state:      │     │ state:      │
            │ STANDBY     │     │ STANDBY     │     │ IDLE        │
            └─────────────┘     └─────────────┘     └─────────────┘
                    │                   │                   │
                    └───────────────────┴───────────────────┘
                                        │
                              Must be stopped to switch
```

### 6.2 ATO Go/Stop Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           ATO State Machine                              │
└─────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐                              ┌──────────────┐
    │   STANDBY    │                              │RUNNING_MOVING│
    │              │                              │              │
    │ active_cab   │         ato_go()             │ ato_running  │
    │ is set       │ ─────────────────────────►   │ = true       │
    │              │                              │              │
    │ ato_running  │     (validate: cab set,      │ train moves  │
    │ = false      │      path exists)            │ toward dest  │
    └──────────────┘                              └──────┬───────┘
           ▲                                             │
           │                                             │
           │           ato_stop()                        │
           │ ◄───────────────────────────────────────────┘
           │
           │      (decelerates to stop,
           │       ato_running = false,
           │       returns to STANDBY)

    Arrival at destination:
    ─────────────────────────
    RUNNING_MOVING ──► RUNNING_AT_STATION ──► STANDBY (dwell complete)
                                              or IDLE (route complete)
```

### 6.3 Direction Indicator Logic

When the cab switches, the visual indicators (triangle for head, square for tail) must update:

```python
def get_head_direction(train_mover, train):
    """
    Get the direction the head of the train is facing.
    
    The head is always in the direction of the active cab.
    When cab switches, facing_direction flips, so head indicator flips.
    """
    if train_mover.active_cab is None:
        return None  # No cab active, no direction
    
    # facing_direction is updated when cab switches
    return train.facing_direction

def get_travel_direction(train_mover, train):
    """
    Get the actual direction the train will move.
    
    In ATO mode: always 'forward' relative to facing_direction
    In Manual mode: can be 'forward' or 'reverse'
    """
    if train_mover.control_mode == 'ato':
        return train.facing_direction  # Head moves forward
    else:
        if train_mover.travel_direction == 'forward':
            return train.facing_direction
        else:
            return OPPOSITE_DIRECTIONS[train.facing_direction]
```

---

## 7. TrainMover Specification

### 7.1 Class Definition

```python
@dataclass
class TrainMover:
    """
    Onboard computer for a train - manages control state and validates commands.
    
    TrainMover is the single authority for:
    - Power state and emergency stop
    - Active cab selection
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction
    - ATO destination, path, and schedule
    - Command validation
    
    It does NOT own:
    - Train position (owned by Train)
    - Block state (owned by BlockManager)
    - Simulation time (owned by SimulationController)
    """
    
    train_id: str
    network: 'RailwayNetwork'
    
    # Power state
    power_on: bool = False
    emergency_stop_active: bool = False
    
    # Onboard computer state
    active_cab: Optional[str] = None  # 'A', 'B', or None
    control_mode: str = 'ato'  # 'ato' or 'manual'
    handle_position: HandlePosition = HandlePosition.N  # Throttle/brake handle
    travel_direction: str = 'forward'  # 'forward' or 'reverse'
    
    # ATO state
    ato_destination_id: Optional[str] = None
    ato_path_id: Optional[str] = None
    ato_schedule_id: Optional[str] = None  # Future: schedule being followed
    ato_running: bool = False
    
    # ─────────────────────────────────────────────────────────────────
    # Power Commands
    # ─────────────────────────────────────────────────────────────────
    
    def power_on_cmd(self) -> CommandResult:
        """
        Turn on train power.
        
        Rules:
        - Train must be in OFF state
        - Transitions to IDLE state
        """
        train = self._get_train()
        
        if self.power_on:
            return CommandResult(False, "Power already on")
        
        self.power_on = True
        train.state = TrainState.IDLE
        self.handle_position = HandlePosition.N
        
        return CommandResult(True)
    
    def power_off_cmd(self) -> CommandResult:
        """
        Turn off train power.
        
        Rules:
        - Train must be stopped
        - Transitions to OFF state
        """
        train = self._get_train()
        
        if not train.is_stopped():
            return CommandResult(False, "Cannot power off while moving")
        
        self.power_on = False
        self.active_cab = None
        self.ato_running = False
        train.state = TrainState.OFF
        
        return CommandResult(True)
    
    def emergency_stop_cmd(self) -> CommandResult:
        """
        Activate emergency stop.
        
        Always allowed - safety override.
        Sets handle to EB, triggers maximum braking.
        """
        self.emergency_stop_active = True
        self.handle_position = HandlePosition.EB
        self.ato_running = False
        
        train = self._get_train()
        train.emergency_brake = True
        
        return CommandResult(True)
    
    def release_emergency_cmd(self) -> CommandResult:
        """
        Release emergency stop.
        
        Rules:
        - Train must be stopped
        - Handle must be in N or B position
        """
        train = self._get_train()
        
        if not train.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")
        
        if self.handle_position not in [HandlePosition.N] + [
            HandlePosition.B1, HandlePosition.B2, HandlePosition.B3,
            HandlePosition.B4, HandlePosition.B5, HandlePosition.B6,
            HandlePosition.B7, HandlePosition.B8
        ]:
            return CommandResult(False, "Handle must be in N or B before releasing emergency")
        
        self.emergency_stop_active = False
        train.emergency_brake = False
        self.handle_position = HandlePosition.N
        
        return CommandResult(True)
    
    # ─────────────────────────────────────────────────────────────────
    # Handle Commands
    # ─────────────────────────────────────────────────────────────────
    
    def set_handle(self, position: HandlePosition) -> CommandResult:
        """
        Set throttle/brake handle position.
        
        Rules:
        - Must be powered on
        - In manual mode: directly controls acceleration/braking
        - In ATO mode: handle is controlled by computer (read-only to user)
        - EB always allowed (safety override)
        """
        if not self.power_on:
            return CommandResult(False, "Power is off")
        
        if position == HandlePosition.EB:
            # EB is always allowed
            return self.emergency_stop_cmd()
        
        if self.emergency_stop_active:
            return CommandResult(False, "Release emergency stop first")
        
        if self.control_mode == 'ato' and self.ato_running:
            return CommandResult(False, "Handle controlled by ATO while running")
        
        self.handle_position = position
        return CommandResult(True)
    
    # ─────────────────────────────────────────────────────────────────
    # Cab Commands
    # ─────────────────────────────────────────────────────────────────
    
    def set_cab(self, cab: Optional[str]) -> CommandResult:
        """
        Set active cab. Flips facing_direction when switching cabs.
        
        Rules:
        - Train must be stopped (speed = 0)
        - Setting cab to 'A' or 'B' transitions to STANDBY
        - Setting cab to None transitions to IDLE
        - Switching from A to B (or vice versa) flips facing_direction
        """
        train = self._get_train()
        
        if not train.is_stopped():
            return CommandResult(False, "Cannot switch cab while moving")
        
        old_cab = self.active_cab
        
        # If switching between A and B, flip facing direction
        if old_cab in ('A', 'B') and cab in ('A', 'B') and old_cab != cab:
            train.facing_direction = OPPOSITE_DIRECTIONS[train.facing_direction]
        
        self.active_cab = cab
        
        # Update train state
        if cab is None:
            train.state = TrainState.IDLE
        else:
            train.state = TrainState.STANDBY
        
        return CommandResult(True)
    
    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).
        
        Rules:
        - Train must be stopped
        - 'ato' → TrainState stays STANDBY (or IDLE if no cab)
        - 'manual' → TrainState becomes MANUAL
        """
        train = self._get_train()
        
        if not train.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")
        
        if mode not in ('ato', 'manual'):
            return CommandResult(False, f"Invalid mode: {mode}")
        
        self.control_mode = mode
        
        if mode == 'manual':
            train.state = TrainState.MANUAL
            self.ato_running = False
        else:
            # Return to STANDBY or IDLE based on cab
            train.state = TrainState.STANDBY if self.active_cab else TrainState.IDLE
        
        return CommandResult(True)
    
    def ato_go(self) -> CommandResult:
        """
        Start ATO movement toward destination.
        
        Rules:
        - Must be in ATO mode
        - Active cab must be set
        - Train must be in STANDBY state
        - Path must be assigned (or will be requested)
        """
        train = self._get_train()
        
        if self.control_mode != 'ato':
            return CommandResult(False, "Not in ATO mode")
        
        if self.active_cab is None:
            return CommandResult(False, "No cab selected")
        
        if train.state != TrainState.STANDBY:
            return CommandResult(False, f"Train not in STANDBY (is {train.state.value})")
        
        # Request path if not already assigned
        if not train.current_path and self.ato_destination_id:
            path_result = self._request_path_to_destination()
            if not path_result.success:
                return path_result
        
        train.state = TrainState.RUNNING_MOVING
        self.ato_running = True
        
        return CommandResult(True)
    
    def ato_stop(self) -> CommandResult:
        """
        Stop ATO movement (service stop, not emergency).
        
        Train will decelerate to stop using the SpeedProfile's
        deceleration_rate, then return to STANDBY.
        
        Note: This does NOT set target_speed directly. Instead,
        it sets ato_running = False and the ATO controller will
        apply braking via the handle.
        """
        train = self._get_train()
        
        if not self.ato_running:
            return CommandResult(False, "ATO not running")
        
        self.ato_running = False
        # ATO controller will see ato_running=False and apply braking
        # State will transition to RUNNING_WAITING then STANDBY when stopped
        
        return CommandResult(True)
    
    def set_travel_direction(self, direction: str) -> CommandResult:
        """
        Set travel direction (forward/reverse) in manual mode.
        
        Rules:
        - Must be in manual mode
        - Train must be stopped
        """
        train = self._get_train()
        
        if self.control_mode != 'manual':
            return CommandResult(False, "Not in manual mode")
        
        if not train.is_stopped():
            return CommandResult(False, "Cannot change direction while moving")
        
        if direction not in ('forward', 'reverse'):
            return CommandResult(False, f"Invalid direction: {direction}")
        
        self.travel_direction = direction
        train.travel_direction = direction
        
        return CommandResult(True)
    
    # ─────────────────────────────────────────────────────────────────
    # Debug Commands (for testing only)
    # ─────────────────────────────────────────────────────────────────
    
    def set_target_speed_debug(self, speed: float) -> CommandResult:
        """
        DEBUG ONLY: Set target speed directly.
        
        This bypasses the handle model and sets a target speed.
        The simulation will automatically apply appropriate
        acceleration/deceleration to reach this speed.
        
        Warning: This is for debugging only. Production code
        should use set_handle() instead.
        """
        train = self._get_train()
        train.target_speed = max(0, min(speed, train.max_speed))
        
        return CommandResult(True, data={"target_speed": train.target_speed})
    
    # ─────────────────────────────────────────────────────────────────
    # Read-only state (for UI)
    # ─────────────────────────────────────────────────────────────────
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize onboard computer state for API/UI."""
        return {
            'train_id': self.train_id,
            'power_on': self.power_on,
            'emergency_stop_active': self.emergency_stop_active,
            'active_cab': self.active_cab,
            'control_mode': self.control_mode,
            'handle_position': self.handle_position.value,
            'travel_direction': self.travel_direction,
            'ato_destination_id': self.ato_destination_id,
            'ato_path_id': self.ato_path_id,
            'ato_schedule_id': self.ato_schedule_id,
            'ato_running': self.ato_running,
        }
```

### 7.2 CommandResult

```python
@dataclass
class CommandResult:
    """Result of a TrainMover command."""
    success: bool
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
```

---

## 8. UI Integration

### 8.1 Data Flow for Train Control Panel

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Train Control Panel Data Sources                    │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                            INFO SECTION                                   │
│  ────────────────────────────────────────────────────────────────────    │
│  Source: Train entity (via StateManager)                                 │
│                                                                          │
│  • State display (OFF/IDLE/STANDBY/RUNNING_MOVING/etc.)                 │
│  • Position (x, y) from train.position.head_tile                        │
│  • Direction from train.facing_direction                                 │
│  • Speed from train.current_speed                                        │
│  • Block from train.block_section_id                                     │
│  • Wait reason from train.wait_reason                                    │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                          CONTROL SECTION                                  │
│  ────────────────────────────────────────────────────────────────────    │
│  Source: TrainMover (via StateManager or direct API)                     │
│                                                                          │
│  • Power state: train_mover.power_on                                    │
│  • Emergency stop: train_mover.emergency_stop_active                    │
│  • Cab buttons state: train_mover.active_cab                            │
│  • Handle position: train_mover.handle_position                         │
│  • Mode toggle state: train_mover.control_mode                          │
│  • ATO Go/Stop enabled: based on train_mover.ato_running + state        │
│  • Travel direction: train_mover.travel_direction                       │
│                                                                          │
│  Button Actions:                                                         │
│  • Power On/Off → POST /api/train/{id}/power                            │
│  • Emergency Stop → POST /api/train/{id}/emergency                      │
│  • Cab A/B/Off → POST /api/train/{id}/cab                               │
│  • Handle P/N/B → POST /api/train/{id}/handle                           │
│  • ATO/Manual → POST /api/train/{id}/mode                               │
│  • Go → POST /api/train/{id}/depart                                     │
│  • Stop → POST /api/train/{id}/stop                                     │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                          DEBUG SECTION                                    │
│  ────────────────────────────────────────────────────────────────────    │
│  Source: Train entity + Path + Block entities (via StateManager)         │
│                                                                          │
│  • Path tiles from train.current_path                                    │
│  • Occupied tiles from train.position.occupied_tiles                    │
│  • Block states from network.blocks (via block_id lookup)               │
│  • ATC clearance/limit from TrainATC                                    │
│  • Lookahead visualization from path + blocks                           │
└──────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Speed Display Update Fix

The speed display must update during simulation. The fix:

```javascript
// In app.js or state-manager.js, on each poll cycle:
function updateTrainControlPanelValues(trainId) {
    const train = stateManager.getTrain(trainId);
    if (!train) return;
    
    // Update speed display
    const speedDisplay = document.querySelector('.train-speed-display');
    if (speedDisplay) {
        speedDisplay.textContent = `${(train.current_speed || 0).toFixed(1)} / ${train.max_speed || 100} km/h`;
    }
    
    // Update state display
    const stateDisplay = document.querySelector('.train-state-display');
    if (stateDisplay) {
        stateDisplay.textContent = (train.state || 'off').toUpperCase().replace('_', ' ');
    }
    
    // Update position display
    const posDisplay = document.querySelector('.train-pos-display');
    if (posDisplay && train.position && train.position.end_a) {
        const headTile = train.position.leading_end === 'a' 
            ? train.position.end_a.tile 
            : train.position.end_b.tile;
        posDisplay.textContent = `(${headTile[0]}, ${headTile[1]})`;
    }
}
```

### 8.3 Direction Indicator Update on Cab Switch

```javascript
// When rendering train on canvas, use facing_direction from Train entity
// The facing_direction is flipped by TrainMover.set_cab() on the backend

function getTrainHeadTailIndicators(train) {
    // Head is always in facing_direction
    const headDirection = train.facing_direction;
    
    // Tail is opposite of head
    const tailDirection = OPPOSITE_DIRECTIONS[headDirection];
    
    return {
        head: {
            direction: headDirection,
            shape: 'triangle',  // Points in travel direction
        },
        tail: {
            direction: tailDirection,
            shape: 'square',
        }
    };
}
```

---

## 9. API Endpoints

### 9.1 TrainMover Command Endpoints

| Endpoint | Method | Description | Request Body |
|----------|--------|-------------|--------------|
| `/api/train/<id>/power` | POST | Power on/off | `{ "power": true \| false }` |
| `/api/train/<id>/emergency` | POST | Emergency stop | `{}` |
| `/api/train/<id>/emergency/release` | POST | Release emergency | `{}` |
| `/api/train/<id>/cab` | POST | Set active cab | `{ "cab": "A" \| "B" \| null }` |
| `/api/train/<id>/handle` | POST | Set handle position | `{ "position": "P5" \| ... \| "EB" }` |
| `/api/train/<id>/mode` | POST | Set control mode | `{ "mode": "ato" \| "manual" }` |
| `/api/train/<id>/depart` | POST | ATO Go | `{}` |
| `/api/train/<id>/stop` | POST | ATO Stop | `{}` |
| `/api/train/<id>/travel-direction` | POST | Set travel direction | `{ "direction": "forward" \| "reverse" }` |
| `/api/train/<id>/target-speed-debug` | POST | DEBUG: Set target speed | `{ "speed": 50.0 }` |

### 9.2 TrainMover State Endpoint

| Endpoint | Method | Description | Response |
|----------|--------|-------------|----------|
| `/api/train/<id>/mover` | GET | Get TrainMover state | Full TrainMover state object |

### 9.3 Response Format

```json
{
    "success": true,
    "error": null,
    "data": {
        "power_on": true,
        "emergency_stop_active": false,
        "active_cab": "A",
        "handle_position": "N",
        "control_mode": "ato",
        "ato_running": true
    }
}
```

---

## 10. Implementation Notes

### 10.1 Tick Driver Location

The simulation tick is driven by `server.py`:

```python
# server.py:150-168
def simulation_loop():
    global last_update_time
    while True:
        current_time = datetime.now()
        delta_seconds = (current_time - last_update_time).total_seconds()
        last_update_time = current_time

        if simulation_running:
            sim_delta = delta_seconds * simulation_speed
            with simulation_lock:
                sim_controller.update(sim_delta)  # ← This calls SimulationController

        time.sleep(0.1)  # 100ms tick rate

# Started as daemon thread
sim_thread = threading.Thread(target=simulation_loop, daemon=True)
sim_thread.start()
```

### 10.2 TrainMover Instance Management

TrainMover instances should be managed per-train, stored in SimulationController:

```python
class SimulationController:
    def __init__(self, network, path_dispatcher=None):
        ...
        self.train_movers: Dict[str, TrainMover] = {}
    
    def get_or_create_mover(self, train_id: str) -> TrainMover:
        if train_id not in self.train_movers:
            self.train_movers[train_id] = TrainMover(
                train_id=train_id,
                network=self.network
            )
        return self.train_movers[train_id]
```

### 10.3 Migration Path

1. **Phase 1:** Add TrainMover class with state properties
2. **Phase 2:** Add command methods with validation
3. **Phase 3:** Wire API endpoints to TrainMover
4. **Phase 4:** Update frontend to use TrainMover state for control display
5. **Phase 5:** Remove duplicated logic from editor-trains.js

### 10.4 Testing Requirements

- Test power on/off transitions
- Test emergency stop always works (even while moving)
- Test emergency release requires stopped + handle in N/B
- Test cab switch only works when stopped
- Test facing_direction flips on cab switch A↔B
- Test handle position changes apply correct acceleration
- Test ATO go validates cab is set
- Test ATO controls handle automatically when running
- Test mode switch only works when stopped
- Test travel direction only changeable in manual mode when stopped
- Test debug target speed API works correctly

---

## Appendix: Glossary

| Term | Definition |
|------|------------|
| **Active Cab** | Which end of the train the "driver" is operating from (A or B) |
| **Facing Direction** | The direction the active cab is pointing (NORTH/EAST/SOUTH/WEST) |
| **Travel Direction** | Whether train moves forward or reverse relative to facing direction |
| **Handle Position** | Throttle/brake lever position: P1-P5 (power), N (neutral), B1-B8 (brake), EB (emergency) |
| **ATO** | Automatic Train Operation - train follows path autonomously with computer-controlled handle |
| **Manual Mode** | User controls handle directly to accelerate/brake |
| **TrainMover** | The onboard computer entity managing train control state |
| **BlockManager** | Centralized authority for block occupancy state |
| **TrainATC** | Automatic Train Control - limits speed for safety |
| **SpeedProfile** | Maps handle positions to acceleration rates and speeds to voltages |
