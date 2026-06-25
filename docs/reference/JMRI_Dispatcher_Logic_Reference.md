# JMRI Dispatcher — Logic Flow Reference

**Source**: JMRI open-source project (`jmri/jmrit/dispatcher/`)
**Relevant version**: JMRI 5.x (analyzed from master branch, June 2026)
**Purpose**: Technical reference for understanding JMRI's multi-train dispatch algorithm, for use in designing compatible or inspired systems.

---

## 1. Overview

The JMRI Dispatcher is an automated multi-train coordinator. Its job is to allocate exclusive ownership of track **Sections** to **ActiveTrains** following predefined routes called **Transits**, while preventing conflicts between trains sharing the same track.

### Key Design Principle

**There is no polling loop.** The Dispatcher is entirely **event-driven**. All decisions run on a single background thread fed by a blocking queue. This serializes every allocation decision and prevents race conditions.

---

## 2. Core Data Model

```
Transit
  └── TransitSection[]       ordered sequence of Sections + metadata
        └── Section
              └── Block[]    one or more contiguous electrical track circuits
                    └── Sensor   occupancy detector (active = train present)
```

| Object | Role |
|--------|------|
| `Transit` | A named route: an ordered sequence of Sections a train will traverse |
| `TransitSection` | One step in a Transit: links a Section + direction + actions |
| `Section` | A named group of Blocks that can be exclusively allocated to one train |
| `Block` | A single electrical detection circuit; has occupancy state (OCCUPIED / UNOCCUPIED) |
| `ActiveTrain` | A train instance currently managed by the Dispatcher; links a Transit to a DCC address |
| `AllocationRequest` | A pending request: "train X needs Section Y next" |
| `AllocatedSection` | A Section currently owned by a train; tracks entry/exit status |
| `AllocationPlan` | A conflict resolution plan for two trains that need the same track |

### ActiveTrain States

```
WAITING ──► RUNNING ◄──► PAUSED
                │    ◄──► WORKING
                │    ◄──► STOPPED
                └──► DONE ──► [terminated or reset to WAITING]
```

### ActiveTrain Modes

| Mode | Who controls the throttle |
|------|---------------------------|
| `AUTOMATIC` | `AutoEngineer` (software), follows signals |
| `MANUAL` | Human operator via DCC throttle |
| `DISPATCHED` | Dispatcher manages routing; human drives |

---

## 3. Architecture: Background Thread + Blocking Queue

`AutoAllocate` implements `Runnable` and runs on a dedicated thread:

```java
// AutoAllocate.run() — simplified
while (!abort) {
    TaskAllocateRelease task = taskList.take(); // BLOCKS until a task arrives
    switch (task.getAction()) {
        case SCAN_REQUESTS:     scanAllocationRequestList(allocationRequests); break;
        case AUTO_RELEASE:      dispatcher.checkAutoRelease();                 break;
        case RELEASE_ONE:       dispatcher.doReleaseAllocatedSection(...);     break;
        case RELEASE_RESERVED:  removeAllReservesForTrain(trainName);          break;
        case ALLOCATE_IMMEDIATE: dispatcher.allocateSection(request, null);    break;
        case ABORT:             abort = true;                                  break;
    }
}
```

### Task Types

| Task | Trigger | Effect |
|------|---------|--------|
| `SCAN_REQUESTS` | Clock tick, new request, section allocated | Re-evaluate all pending AllocationRequests |
| `AUTO_RELEASE` | Clock tick, section exited | Release sections the train has fully passed through |
| `RELEASE_ONE` | Train terminates or dispatcher action | Release one specific AllocatedSection |
| `RELEASE_RESERVED` | Train terminates | Clear all soft reservations held for a train |
| `ALLOCATE_IMMEDIATE` | Manual dispatcher action | Force-allocate one specific AllocationRequest |

---

## 4. Event Triggers

### Trigger A: Fast Clock Minute Tick

`DispatcherFrame.newFastClockMinute()` fires on every simulated minute:

```
FOR EACH train in delayedTrains:
  IF not started AND delay type is TIMEDDELAY:
    IF fastClock time >= departure time:
      → mark train as started, remove from delayed list
  ELSE IF started AND status is READY AND at restart point:
    IF fastClock time >= restart departure time:
      → restart train

IF AutoAllocate enabled:
  → queue SCAN_REQUESTS
```

### Trigger B: Section Lifecycle Events

These queue tasks whenever track ownership changes:

| Event | Task queued |
|-------|-------------|
| New `AllocationRequest` created | `SCAN_REQUESTS` |
| Section successfully allocated | `SCAN_REQUESTS` |
| Section released (auto) | `AUTO_RELEASE` + `SCAN_REQUESTS` |
| Section released (manual) | `SCAN_REQUESTS` |
| Train terminates | `RELEASE_RESERVED` + `RELEASE_ONE` per section |

---

## 5. The Allocation Scan: `scanAllocationRequestList()`

This is the core decision engine. It runs every time `SCAN_REQUESTS` is processed.

### Step 1: Sort and Clean

```
1. Copy all AllocationRequests into a working list
2. Sort by train priority (highest priority first; ties broken alphabetically by name)
3. Remove any AllocationPlans that have been fully satisfied
```

### Step 2: Evaluate Each Request (in priority order)

For each `AllocationRequest`:

**Pre-checks (skip this request if any are true):**

| Check | Condition to skip |
|-------|-------------------|
| Stop-allocate sensor | A sensor on the last TransitSection is ACTIVE |
| Train held | `holdAllocation()` is true |
| Delayed start | Train hasn't started yet and uses TIMEDDELAY or sensor-based delay |
| Signal held (SignalMast mode) | The signal mast protecting the section entry is in HELD state |
| Section reserved for another train | `reservedSections` map has this section assigned to a different train |

**Allocation Path A — Allocate by Safe Sections:**

Used when `allocateMethod == ALLOCATE_BY_SAFE_SECTIONS`.

```
Look ahead from the requested section through the Transit until a "safe" section
(a section with alternate track — a passing siding) is found.

FOR each section between here and the next safe section:
  IF section is not FREE → remove all reservations for this train, skip
  IF section is OCCUPIED → remove all reservations for this train, skip
  IF section's blocks are in another train's allocated section → skip
  IF section is reserved for another train → skip
  ELSE → soft-reserve this section for this train

IF all forward sections are clear:
  → call allocateMore() to perform the actual allocation
  → remove reservation for the just-allocated section
```

**Allocation Path B — Regular Allocation:**

```
IF section is FREE AND UNOCCUPIED:
  IF this is the only active train:
    → allocate immediately

  ELSE:
    Build a list of other trains that also need this section (or its alternates)

    FOR EACH conflicting train:
      IF trains move in SAME direction:
        IF this train leads, but conflicting train has HIGHER priority:
          → checkForPassingPlan() — create a PASSING_MEET plan

      IF trains move in OPPOSITE directions:
        IF trains will eventually reach each other's positions:
          → checkForXingPlan() — create a XING_MEET plan

    IF no conflicts (or conflicts resolved by plans):
      → allocate
```

---

## 6. Conflict Resolution: AllocationPlans

When two trains would conflict, an `AllocationPlan` is created instead of allocating. The plan tells future scan iterations where each train should go.

### XING_MEET (opposite-direction conflict)

**Scenario**: Train A and Train B are heading toward each other on single track.

```
1. Find a passing siding (a TransitSection with alternate sections) in either train's Transit
2. Assign Train A to one alternate (e.g., the siding track)
3. Assign Train B to the other alternate (e.g., the main track)
4. Store as AllocationPlan with targetSection for each train
5. Future SCAN_REQUESTS: each train allocates only up to its targetSection
6. Plan is removed once both trains have reached their target sections
```

**Guards against gridlock:**
- If a third train is between A and B, no plan is created
- If another plan already uses the same passing section, no plan is created

### PASSING_MEET (same-direction, priority difference)

**Scenario**: A high-priority train is behind a lower-priority train, both going the same direction.

```
1. Find a passing siding ahead in either train's Transit
2. Direct the lower-priority (slower/ahead) train to the siding track
3. Direct the higher-priority train to the main track, one section further ahead
4. Store as AllocationPlan
5. Future SCAN_REQUESTS: trains allocate up to their respective target sections
6. Plan removed once both trains have cleared the siding
```

### Plan Lifecycle

```
Created: when scanAllocationRequestList() detects a conflict
Active:  future scan iterations check willAllocatingFollowPlan() before allocating
Removed: when AllocationPlan.isComplete() returns true (all planned sections allocated)
```

---

## 7. `allocateMore()` — Allocation Depth Control

Before actually allocating, `allocateMore()` checks how far ahead sections have already been allocated, and enforces the configured look-ahead limit.

| `allocateMethod` value | Behavior |
|------------------------|----------|
| `ALLOCATE_AS_FAR_AS_IT_CAN` | Allocate as many sections ahead as possible |
| `ALLOCATE_BY_SAFE_SECTIONS` | Allocate up to the next passing point (safe section) |
| N (integer) | Allocate at most N sections ahead of the train's current occupied section |

For the N-ahead mode:
```
Count sections that are allocated but not yet occupied (train hasn't entered them)
IF count < N: → allocate one more
ELSE: → do not allocate, return false
```

---

## 8. Section Allocation: `allocateSection()`

When `allocateMore()` approves allocation, `DispatcherFrame.allocateSection()` performs the actual work:

```
1. Validate: section must be FREE
2. Validate: section must not be occupied (unless dispatcher overrides)
3. Validate: no blocks in the section are already in another train's AllocatedSection
4. Determine direction of travel through the section
5. Set turnouts via AutoTurnouts (uses Layout Editor connectivity to find required positions)
6. Create AllocatedSection object:
   - Link to ActiveTrain
   - Set up block occupancy listeners
   - Set up "stopping sensor" if configured
7. Mark Section state as ALLOCATED (in the direction of travel)
8. Add to train's allocatedSectionList
9. Remove AllocationRequest from pending list
10. Queue SCAN_REQUESTS (allocation may unblock other trains)
```

---

## 9. Section Release: `checkAutoRelease()`

Auto-release runs when `AUTO_RELEASE` is processed:

```
FOR EACH AllocatedSection across ALL active trains:
  IF train has ENTERED the section AND has EXITED the section:
    IF train still has sections allocated ahead:
      → release this section
      → queue SCAN_REQUESTS (freed section may unblock other trains)
```

An `AllocatedSection` tracks entry/exit via block occupancy listeners:
- **Entered**: the first block in the section becomes OCCUPIED
- **Exited**: the last block in the section becomes UNOCCUPIED (train rear has cleared)

---

## 10. Reservation System

The soft-reservation map prevents section contention during look-ahead planning:

```java
Map<String, String> reservedSections
// key: Section system name
// value: train name that reserved it
```

- Reservations are created during Allocation Path A (safe-sections look-ahead)
- Reservations are checked in pre-checks before allocating
- Reservations are released when a section is allocated (reservation → actual allocation)
- All reservations for a train are cleared when the train terminates (`RELEASE_RESERVED`)

---

## 11. Automatic Train Control (AutoActiveTrain / AutoEngineer)

For trains in AUTOMATIC mode, a separate control loop manages the throttle.

### Speed Levels

| Constant | Throttle % | Signal Aspect |
|----------|-----------|---------------|
| `STOP_SPEED` | 0% | Stop / Dark |
| `RESTRICTED_SPEED` | 25% | Restricted |
| `SLOW_SPEED` | 35% | Slow |
| `MEDIUM_SPEED` | 50% | Medium |
| `LIMITED_SPEED` | 65% | Limited |
| `NORMAL_SPEED` | 80% | Clear |
| `MAXIMUM_SPEED` | 115% | (override) |

### Speed Ramp Modes

| Mode | Behavior |
|------|----------|
| `RAMP_NONE` | Instant speed change |
| `RAMP_FAST` | Fast acceleration/deceleration |
| `RAMP_MEDIUM` | Medium rate |
| `RAMP_SLOW` | Slow, prototypical rate |
| `RAMP_SPEEDPROFILE` | Uses decoder speed profile curve |
| `RAMP_PHYSICS` | Physics model (weight, rolling resistance) |

### Control Loop (per train, event-driven)

```
On signal aspect change ahead:
  → map aspect to target speed constant
  → begin ramp toward target speed at configured rate

On block occupancy change:
  → update train position
  → check if approaching stopping sensor
  → recalculate distance to next stop

On section exit detected:
  → notify Dispatcher (triggers AUTO_RELEASE)
  → request next section AllocationRequest
```

---

## 12. Complete Event Flow Example

```
[Setup]
User activates train → ActiveTrain created, AllocationRequest for Section 1 queued
→ SCAN_REQUESTS processed → Section 1 allocated, turnouts set
→ AllocationRequest for Section 2 queued → SCAN_REQUESTS → Section 2 allocated
→ (repeat N sections ahead based on allocateMethod)

[Train running]
Train enters Section 1 → AllocatedSection.entered = true
Train exits Section 1 → AllocatedSection.exited = true
  → AUTO_RELEASE: Section 1 released, state = FREE
  → SCAN_REQUESTS: any train waiting for Section 1 may now allocate
  → AllocationRequest for Section 3 queued → SCAN_REQUESTS → allocated

[Fast clock tick]
→ Check delayed trains → none ready
→ SCAN_REQUESTS: all pending requests re-evaluated (idempotent if nothing changed)

[Second train added, conflict detected]
Second train AllocationRequest for Section 4 queued
→ SCAN_REQUESTS: Train A also needs Section 4, trains moving opposite directions
→ willTrainsCross() = true
→ checkForXingPlan(): find passing siding at Section 5, create AllocationPlan
→ Train A directed to Section 5 main; Train B directed to Section 5 siding
→ Each allocates only up to their target section
→ After both pass Section 5, AllocationPlan removed, normal allocation resumes

[Train terminates]
→ RELEASE_RESERVED: clear all soft reservations for this train
→ RELEASE_ONE for each AllocatedSection: sections returned to FREE
→ SCAN_REQUESTS: other trains may unblock
```

---

## 13. Key Design Decisions (and Rationale)

| Decision | Rationale |
|----------|-----------|
| Single background thread | Prevents race conditions on shared allocation state without fine-grained locking |
| Blocking queue for tasks | Decouples event producers (clock, sensors) from allocation logic; tasks are processed in order |
| Conservative allocation | If any doubt about safety, don't allocate — prevents deadlocks at the cost of occasionally waiting longer than necessary |
| Soft reservations separate from allocations | Allows look-ahead planning without actually locking sections; failed plans don't leave sections stuck |
| AllocationPlan objects | Externalizes conflict state — avoids re-computing meet logic every scan cycle; plans persist until satisfied |
| Priority-sorted scan order | Higher-priority trains always evaluated first; ensures priority is respected even under contention |
| Event-driven (no poll loop) | Low CPU overhead; response time tied to hardware sensor latency, not poll interval |

---

## 14. Source File Map

| File | Responsibility |
|------|----------------|
| `DispatcherFrame.java` | Main window, ActiveTrain lifecycle, section allocation/release logic, fast clock listener |
| `AutoAllocate.java` | Background thread, task queue, `scanAllocationRequestList()`, conflict plan creation |
| `ActiveTrain.java` | Train state machine, allocated section list, transit position tracking |
| `AutoActiveTrain.java` | Throttle management, speed control, signal following (extends ActiveTrain) |
| `AutoTrainAction.java` | Executes TransitSectionActions (speed changes, sensor waits, work stops) |
| `AutoTurnouts.java` | Determines and sets required turnout positions for a section |
| `AllocationRequest.java` | Pending request: train + section + direction + sequence number |
| `AllocatedSection.java` | Active ownership: section + train + entry/exit state + block listeners |
| `AllocationPlan.java` | Meet plan: two trains + their target sections + plan type |
| `TaskAllocateRelease.java` | Queue task wrapper: action enum + payload |
| `TrainInfo.java` | Serializable train configuration (XML persistence) |
| `TrainInfoFile.java` | Read/write `dispatcher/traininfo/*.xml` |
| `OptionsFile.java` | Read/write `dispatcher/dispatcheroptions.xml` |

---

*Document generated from JMRI source analysis, June 2026.*
*JMRI is licensed under GNU GPL v2. See https://www.jmri.org*
