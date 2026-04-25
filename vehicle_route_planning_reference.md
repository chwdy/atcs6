# OpenTTD Vehicle Route Planning — Reference Document

## 1. Data Model

### 1.1 The `Order` Struct (`src/order_base.h:33`)

Each entry in a vehicle's schedule is an `Order` value object (not pool-allocated itself). Key fields:

| Field | Type | Meaning |
|---|---|---|
| `type` | `uint8_t` | Bits 0–3 = `OrderType`; bits 4–5 = stop location (station orders); bits 6–7 = non-stop flags |
| `flags` | `uint8_t` | Bits 0–2 = unload type; bit 3 = wait timetabled; bits 4–6 = load type; bit 7 = travel timetabled |
| `dest` | `DestinationID` | Wraps `uint16_t`; doubles as `StationID`, `DepotID`, or waypoint ID depending on order type |
| `refit_cargo` | `CargoType` | Cargo to refit to, or `CARGO_NO_REFIT` |
| `wait_time` | `uint16_t` | Ticks to wait at destination (timetable) |
| `travel_time` | `uint16_t` | Ticks the journey should take (timetable) |
| `max_speed` | `uint16_t` | Speed cap (km-ish/h) while travelling to destination |

**Order types** (`src/order_type.h:50`):

| Enum | Value | Role |
|---|---|---|
| `OT_NOTHING` | 0 | No active order (transient / cleared) |
| `OT_GOTO_STATION` | 1 | Travel to a station; stop, load, unload |
| `OT_GOTO_DEPOT` | 2 | Travel to a depot; service and/or halt |
| `OT_LOADING` | 3 | Currently loading/unloading at a station (transient run-time state) |
| `OT_LEAVESTATION` | 4 | One-tick transient: vehicle just finished loading |
| `OT_DUMMY` | 5 | Placeholder / cleared in-flight order |
| `OT_GOTO_WAYPOINT` | 6 | Travel to a waypoint; pass through without stopping |
| `OT_CONDITIONAL` | 7 | Conditional branch: skip to another order based on vehicle variables |
| `OT_IMPLICIT` | 8 | Auto-inserted when a ground vehicle stops at an unscheduled station |

**Stops vs. waypoints** — the distinction is encoded in `OrderType`:
- `OT_GOTO_STATION` = full stop (load/unload).
- `OT_GOTO_WAYPOINT` = pass-through; vehicle does not load/unload.
- `OT_GOTO_STATION` with `OrderNonStopFlag::NoDestination` set acts as a *via* station — the vehicle passes through without loading.

**Non-stop flags** (`OrderNonStopFlags`, bits 6–7 of `Order::type`):
- `NoIntermediate` — skip intermediate stations; go straight to destination (classic non-stop).
- `NoDestination` — skip loading at the destination itself (*via* mode).

### 1.2 The `OrderList` Struct (`src/order_base.h:274`)

Holds the complete ordered schedule shared by one or more vehicles. Pool-allocated via `OrderListPool` (max 64 000 lists, pool ID `OrderListID`).

| Field | Type | Meaning |
|---|---|---|
| `orders` | `std::vector<Order>` | The flat, zero-indexed sequence of orders |
| `num_manual_orders` | `VehicleOrderID` | Count of explicitly added (non-implicit) orders |
| `num_vehicles` | `uint` | Count of vehicles sharing this list |
| `first_shared` | `Vehicle *` | Head of the shared-vehicle linked list |
| `timetable_duration` | `TimerGameTick::Ticks` | Sum of timetabled travel+wait times |
| `total_duration` | `TimerGameTick::Ticks` | Sum of all travel+wait times (timetabled or measured) |

**Key accessors:**
- `GetOrderAt(index)` — `O(1)` random access into `orders` vector.
- `GetNext(cur)` — returns `(cur + 1) % GetNumOrders()`. Always cyclic.
- `GetNumOrders()` — `orders.size()`.

### 1.3 Vehicle-side order fields (`src/vehicle_base.h:311–316`, `src/base_consist.h:47–48`)

Located in `Vehicle` (the live/transient part) and `BaseConsist` (persist across autoreplacement):

| Field | Location | Meaning |
|---|---|---|
| `current_order` | `Vehicle` | The order the vehicle is *actively executing right now* (copied from the list) |
| `orders` | `Vehicle` | Pointer to the shared `OrderList`; `nullptr` if no orders |
| `cur_real_order_index` | `BaseConsist` | Index of the next *explicit* (non-implicit) order to be reached |
| `cur_implicit_order_index` | `BaseConsist` | Index in the full list (including implicit orders) currently being followed |
| `current_order_time` | `BaseConsist` | Ticks elapsed since the current order began |
| `lateness_counter` | `BaseConsist` | Ticks late vs. timetable (negative = early) |
| `round_trip_time` | `BaseConsist` | Measured ticks for one full circumnavigation (used for depot unbunching only) |

### 1.4 Run Mode — Cyclic Only

OpenTTD has **no built-in round-trip / ping-pong mode**. The schedule is always traversed forward and wraps at the end via `GetNext()`. A "return journey" is achieved by the player manually adding reverse-leg orders to the list.

---

## 2. Schedule / Stop Logic

### 2.1 Dual Order Indices

Two indices coexist to support *implicit orders* (auto-inserted stops):

- **`cur_real_order_index`** — always points at a non-`OT_IMPLICIT` order (a manual order). This is the "goal" the vehicle is navigating toward.
- **`cur_implicit_order_index`** — points at the order currently being executed, which may be `OT_IMPLICIT`. Normally equals `cur_real_order_index`; diverges when the vehicle has implicit intermediate stops.

### 2.2 Advancement Functions (`src/vehicle_base.h:806–883`)

| Function | What it does |
|---|---|
| `SkipToNextRealOrderIndex()` | Increments `cur_real_order_index`, skipping past any `OT_IMPLICIT` entries, with wraparound. Internal. |
| `IncrementImplicitOrderIndex()` | Advances `cur_implicit_order_index` to the next implicit (or real) order. Also advances the real index if both were equal. Invalidates the order GUI. |
| `IncrementRealOrderIndex()` | Advances `cur_real_order_index` to the next explicit order. Drags `cur_implicit_order_index` along if they were equal. |
| `UpdateRealOrderIndex()` | Corrects `cur_real_order_index` to skip past any implicit orders (called after external index changes). |

**Wraparound**: both indices wrap at `GetNumOrders()` → `0`. This is always cyclic; there is no direction reversal.

### 2.3 Order Advancement Trigger

After finishing at a stop, the call chain is:

```
HandleLoading()            // vehicle.cpp:2407
  → LeaveStation()         // sets current_order = OT_LEAVESTATION
  → advance cur_implicit_order_index (IncrementImplicitOrderIndex)
    [only if that order was the scheduled stop]

TrainLocoHandler() / per-type controller
  → ProcessOrders()        // order_cmd.cpp:2075
      → sees OT_LEAVESTATION or OT_NOTHING
      → reads next order from cur_real_order_index
      → copies it into current_order
      → calls UpdateOrderDest() to set dest_tile
```

---

## 3. Vehicle Assignment

### 3.1 Linking a Vehicle to an OrderList

`Vehicle::orders` (`vehicle_base.h:314`) holds a raw pointer to the `OrderList`. The `OrderList` owns a doubly-linked list of sharing vehicles via:
- `Vehicle::next_shared` / `Vehicle::previous_shared`
- `OrderList::first_shared`

### 3.2 Sharing Orders (`CO_SHARE`, `src/order_cmd.cpp:1503`)

`CmdCloneOrder` with `CO_SHARE`:
1. Points `dst->orders = src->orders` (same pool object).
2. Calls `dst->AddToShared(src)` — inserts `dst` into the shared vehicle chain.
3. `OrderList::num_vehicles` increments.

`CO_COPY` (`order_cmd.cpp:1560`) creates a fresh `OrderList` with cloned orders; independent thereafter.

`CO_UNSHARE` splits a vehicle off the shared list (`DeleteVehicleOrders()` + `RemoveFromShared()`).

### 3.3 Unassigning / Going to Depot

When a player sends a vehicle to the depot (`CMD_SEND_VEHICLE_TO_DEPOT`), `Vehicle::SendToDepot()` (`vehicle.cpp:2555`) is called:

1. Calls the virtual `FindClosestDepot()` (see §3.4) to locate the nearest depot.
2. Sets `current_order` to `OT_GOTO_DEPOT` with `OrderDepotActionFlag::Halt` (or `Service`).
3. Sets `dest_tile` to the depot tile.
4. For trains: if the depot is behind, `CMD_REVERSE_TRAIN_DIRECTION` is issued automatically.

Once the vehicle arrives, `VehicleEnterDepot()` (`vehicle.cpp:1520`) is called:
- Sets `VehState::Hidden`.
- Calls `VehicleServiceInDepot()`.
- If `OrderDepotActionFlag::Halt` is set → vehicle stays stopped.
- If `OrderDepotTypeFlag::PartOfOrders` → `IncrementRealOrderIndex()` and the vehicle resumes normal orders.

**When orders are deleted** and none remain, `ProcessOrders()` calls `current_order.Free()` and `SetDestTile(0)`. The vehicle coasts to a stop. The player must then manually send it to a depot or add orders.

### 3.4 Finding the Home Depot

There is **no stored "home depot"** on a vehicle. `FindClosestDepot()` is called dynamically:

| Vehicle type | Implementation | Pathfinder |
|---|---|---|
| Train | `Train::FindClosestDepot()` → `FindClosestTrainDepot()` → `YapfTrainFindNearestDepot()` | YAPF |
| Road vehicle | `RoadVehicle::FindClosestDepot()` → `YapfRoadVehicleFindNearestDepot()` | YAPF |
| Ship | `Ship::FindClosestDepot()` → `YapfShipFindNearestDepot()` | YAPF |
| Aircraft | `Aircraft::FindClosestDepot()` → search for nearest airport with hangar | proximity |

Returns `ClosestDepot { TileIndex location, DestinationID destination, bool reverse, bool found }`.

Automatic servicing (`CheckIfTrainNeedsService()`, `train_cmd.cpp:4139`) also calls `FindClosestDepot()` each economy day, subject to `maximum_go_to_depot_penalty`.

---

## 4. Jump-to-Stop

### 4.1 The Command

**`CMD_SKIP_TO_ORDER`** → `CmdSkipToOrder(DoCommandFlags, VehicleID, VehicleOrderID)` (`src/order_cmd.cpp:1035`, declared in `src/order_cmd.h:27`).

```
DEF_CMD_TRAIT(CMD_SKIP_TO_ORDER, CmdSkipToOrder, CommandFlag::Location, CommandType::RouteManagement)
```

### 4.2 UI Trigger (`src/order_gui.cpp:712`)

In the Order window, the **Skip button** (`WID_O_SKIP`):
- **Plain click** → skip to `(cur_implicit_order_index + 1) % num_orders`.
- **Ctrl + click** → skip to the currently *selected* order in the list.

Both paths post `CMD_SKIP_TO_ORDER` with the target index.

### 4.3 What the Command Does (`order_cmd.cpp:1035`)

```cpp
v->cur_implicit_order_index = v->cur_real_order_index = sel_ord;
v->UpdateRealOrderIndex();
```

If the vehicle was loading (`OT_LOADING`), `LeaveStation()` is called first to cleanly exit.

### 4.4 Teleport vs. Pathfind

**No teleportation.** The vehicle's physical position is unchanged. Only the target order changes. On the vehicle's next movement tick, `ProcessOrders()` picks up the new order, `UpdateOrderDest()` resolves the new `dest_tile`, and YAPF pathfinds from the vehicle's current tile to the new destination.

---

## 5. Pathfinding Integration

### 5.1 Engine: YAPF (Yet Another Pathfinder)

All land/water vehicle types use YAPF, located in `src/pathfinder/yapf/`. Entry points are declared in `src/pathfinder/yapf/yapf.h`.

| Function | Vehicle | Call site |
|---|---|---|
| `YapfTrainChooseTrack()` | Train | `DoTrainPathfind()` → `ChooseTrainTrack()`, train_cmd.cpp:2502 |
| `YapfRoadVehicleChooseTrack()` | Road vehicle | roadveh_cmd.cpp:993 |
| `YapfShipChooseTrack()` | Ship | ship_cmd.cpp:491 |
| `YapfTrainFindNearestDepot()` | Train | `FindClosestTrainDepot()`, train_cmd.cpp:2200 |
| `YapfRoadVehicleFindNearestDepot()` | Road vehicle | roadveh_cmd.cpp:348 |
| `YapfTrainFindNearestSafeTile()` | Train | PBS path extension |
| `YapfTrainCheckReverse()` | Train | Check if reversing at station is faster |

### 5.2 When Pathfinding Runs

**Not at assignment time.** YAPF runs on-demand, tile by tile:

- **Trains**: `ChooseTrainTrack()` is called every time the train reaches a tile with a track choice (junction, signal). YAPF selects the best track toward `current_order.GetDestination()`. With PBS signals enabled, a path reservation is extended ahead (`TryPathReserve()`), but this reservation is stored in the map's signal state, not on the vehicle.

- **Ships**: `YapfShipChooseTrack()` is called when the path cache runs empty. YAPF fills `Ship::path` (`std::vector<ShipPathElement>`, `ship.h:33`) with upcoming trackdirs; the vehicle consumes one element per tile step.

- **Road vehicles**: Same pattern. `YapfRoadVehicleChooseTrack()` fills `RoadVehicle::path` (`std::vector<RoadVehPathElement>`, `roadveh.h:99`) with (tile, trackdir) pairs; consumed step by step.

### 5.3 Path Cache Summary

| Vehicle | Cache field | Cache type | Invalidated when |
|---|---|---|---|
| Train | None on vehicle; PBS reservation on map tiles | — | Signal state changes |
| Ship | `Ship::path` | `ShipPathCache` (vector of `ShipPathElement`) | Consumed; refilled by YAPF at each empty check |
| Road vehicle | `RoadVehicle::path` | `RoadVehPathCache` (vector of `RoadVehPathElement`) | Consumed; refilled by YAPF at each empty check |

After `CmdSkipToOrder`, the vehicle's path cache is implicitly stale; YAPF replans from the next tile-step opportunity.

### 5.4 The `current_order` as Pathfinding Target

`UpdateOrderDest()` (`order_cmd.cpp:1946`) translates the current order into `dest_tile`:
- `OT_GOTO_STATION` → `v->GetOrderStationLocation(station_id)` (nearest station tile to approach from)
- `OT_GOTO_WAYPOINT` → `Waypoint::Get(...)->xy`
- `OT_GOTO_DEPOT` (specific) → `Depot::Get(...)->xy`
- `OT_GOTO_DEPOT` (nearest) → calls `FindClosestDepot()` then sets `dest_tile`
- `OT_CONDITIONAL` → evaluates condition, jumps order index, recurses

YAPF uses `dest_tile` as the heuristic target.

---

## 6. State Machine

OpenTTD does not have a single `enum` for all vehicle states. State is distributed across three orthogonal mechanisms:

### 6.1 `current_order.GetType()` — Primary Order State

| State (OrderType) | Meaning |
|---|---|
| `OT_NOTHING` | No active order; vehicle is idle or between orders |
| `OT_GOTO_STATION` | En route to a station |
| `OT_GOTO_WAYPOINT` | En route to a waypoint |
| `OT_GOTO_DEPOT` | En route to (or seeking) a depot |
| `OT_LOADING` | Arrived at station; loading/unloading in progress |
| `OT_LEAVESTATION` | One-tick transient: cargo transfer done, vehicle about to depart |
| `OT_DUMMY` | Order cleared mid-flight (depot orders can clear themselves) |
| `OT_CONDITIONAL` | Evaluating condition; will jump and resolve immediately |

### 6.2 `vehstatus` Bits — Physical State

`Vehicle::vehstatus` (`VehStates`, `vehicle_base.h:42`):
- `VehState::Stopped` — player-stopped; vehicle will not move.
- `VehState::Hidden` — vehicle is inside a depot (not rendered).
- `VehState::Crashed` — vehicle has crashed.

### 6.3 Vehicle-type-specific State

- **Train** `VehicleRailFlags` (`train.h:25`): `Reversing`, `Stuck`, `LeavingStation`, etc.
- **Aircraft** `AirportMovementStates` (`airport.h:62`): `FLYING`, `LANDING`, `ENDLANDING`, `HELILANDING`, `HANGAR`, …
- **Road vehicle** `state` byte using `RoadVehicleStates`.

### 6.4 State Transition Flow (Train as canonical example)

Per-tick entry point: `Train::Tick()` → `TrainLocoHandler()` (`train_cmd.cpp:3942`).

```
[Depot / Stopped]
    ↓  player starts vehicle / order with OT_GOTO_STATION
[En route]  current_order = OT_GOTO_STATION / OT_GOTO_WAYPOINT / OT_GOTO_DEPOT
    │  TrainController() moves train tile by tile
    │  YAPF invoked at each junction (ChooseTrainTrack)
    ↓  train enters station tile → TrainEnterStation() → BeginLoading()
[Loading]   current_order = OT_LOADING
    │  HandleLoading() polls VehicleFlag::LoadingFinished + timetable wait_time
    ↓  loading done → LeaveStation()
[Leaving]   current_order = OT_LEAVESTATION
    │  LeavingStation rail flag set; path reservation attempted
    ↓  ProcessOrders() sees OT_LEAVESTATION → clears to OT_NOTHING → picks next order
[En route]  current_order = OT_GOTO_STATION (next stop)
    ...
    ↓  order is OT_GOTO_DEPOT → VehicleEnterDepot()
[In Depot]  VehState::Hidden set; VehicleServiceInDepot()
    │  if OrderDepotActionFlag::Halt → stays stopped (VehState::Stopped)
    │  if PartOfOrders → IncrementRealOrderIndex(), resumes orders
    ↓
[En route]
```

### 6.5 Key Transition Functions

| Transition | Function | File |
|---|---|---|
| Arrive at station → Loading | `Vehicle::BeginLoading()` | `vehicle.cpp:2183` |
| Loading → Leaving station | `Vehicle::LeaveStation()` | `vehicle.cpp:2327` |
| Leaving → En route (next order) | `ProcessOrders()` | `order_cmd.cpp:2075` |
| En route → Order resolved | `UpdateOrderDest()` | `order_cmd.cpp:1946` |
| Arrive at depot | `VehicleEnterDepot()` | `vehicle.cpp:1520` |
| Order index advance | `IncrementRealOrderIndex()` | `vehicle_base.h:854` |
| Conditional branch | `ProcessConditionalOrder()` (inside `UpdateOrderDest`) | `order_cmd.cpp` |
| Handle loading tick | `Vehicle::HandleLoading()` | `vehicle.cpp:2407` |

---

## Key File Index

| File | Role |
|---|---|
| `src/order_type.h` | All order-related enums (OrderType, OrderLoadType, OrderNonStopFlag, …) |
| `src/order_base.h` | `Order` and `OrderList` struct definitions |
| `src/order_cmd.h` | Command declarations for CMD_SKIP_TO_ORDER, CMD_MODIFY_ORDER, etc. |
| `src/order_cmd.cpp` | All order command implementations; `ProcessOrders()`, `UpdateOrderDest()`, `CmdSkipToOrder()`, `CmdCloneOrder()` |
| `src/order_gui.cpp` | Order window UI; Skip button, `OrderClick_Skip()` |
| `src/base_consist.h` | `BaseConsist` with `cur_real_order_index`, `cur_implicit_order_index`, timetable fields |
| `src/vehicle_base.h` | `Vehicle` struct; `IncrementRealOrderIndex()`, `IncrementImplicitOrderIndex()`, `GetOrder()` |
| `src/vehicle.cpp` | `BeginLoading()`, `LeaveStation()`, `HandleLoading()`, `VehicleEnterDepot()`, `SendToDepot()` |
| `src/train.h` | `Train` struct; `VehicleRailFlags` |
| `src/train_cmd.cpp` | `TrainLocoHandler()`, `TrainEnterStation()`, `ChooseTrainTrack()`, `DoTrainPathfind()`, `CheckIfTrainNeedsService()`, `Train::FindClosestDepot()` |
| `src/roadveh.h` | `RoadVehicle` struct; `RoadVehPathCache` |
| `src/roadveh_cmd.cpp` | Road vehicle movement; `YapfRoadVehicleChooseTrack()` call site |
| `src/ship.h` | `Ship` struct; `ShipPathCache` |
| `src/ship_cmd.cpp` | Ship movement; `YapfShipChooseTrack()` call site |
| `src/aircraft.h` | `Aircraft` struct; `AirportMovementStates` |
| `src/pathfinder/yapf/yapf.h` | YAPF public API: `YapfTrainChooseTrack()`, `YapfRoadVehicleChooseTrack()`, `YapfShipChooseTrack()`, `YapfTrainFindNearestDepot()`, etc. |
| `src/pathfinder/yapf/yapf_rail.cpp` | Train YAPF implementation |
| `src/pathfinder/yapf/yapf_road.cpp` | Road vehicle YAPF implementation |
| `src/pathfinder/yapf/yapf_ship.cpp` | Ship YAPF implementation |
