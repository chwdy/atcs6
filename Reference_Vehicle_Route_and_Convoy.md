# Simutrans: Vehicle Route, Schedule & Convoy — Reference Document

---

## Part 1: How Convoys Work

### What is a convoy?

A **convoy** (`convoi_t`, `simconvoi.h`) is the fundamental moving unit in Simutrans. It is a group of one or more vehicles (wagons, cars, ships, planes) that move as a single object, share one schedule, follow one route, and are managed together as one entity. A single diesel locomotive pulling three freight wagons is one convoy. A standalone bus is also one convoy.

### Composition: `fahr[]` and `vehicle_count`

`fahr` (`simconvoi.h:183`) is a dynamic array of `vehicle_t*` pointers. `vehicle_count` (`simconvoi.h:178`) is how many are actually in it.

- `fahr[0]` is the **lead vehicle** (the engine at the front)
- `fahr[vehicle_count-1]` is the **tail vehicle** (the last wagon)
- Vehicles in between are followers

Adding a vehicle: `convoi_t::add_vehicle(vehicle_t*, bool infront)` (`simconvoi.cc:1674`)
- `infront=true` inserts at `fahr[0]`, shifts everything else back
- `infront=false` appends at the tail
- Each addition immediately updates convoy-wide aggregates: `sum_power`, `sum_weight`, `min_top_speed`, `sum_running_costs`, etc.

Removing: `convoi_t::remove_vehicle_at(uint16 i)` (`simconvoi.cc:1740`) closes the gap, subtracts the vehicle's contributions, and recalculates speed.

After every add/remove, `set_erstes_letztes()` (`simconvoi.cc:1823`) refreshes the `leading` and `last` flags on `fahr[0]` and `fahr[vehicle_count-1]`.

### What `fahr[0]` (the lead vehicle) does

The lead vehicle carries extra responsibilities:

- **Pathfinding**: only `fahr[0]` calls `calc_route()`. The route is stored on `convoi_t`, not on the vehicle. All vehicles read it via `cnv->get_route()`.
- **Tile-entering checks**: `vehicle_t::hop_check()` (`vehicle.cc:411`) only runs the full check (signals, one-way signs, blocking, halt detection) for the lead. Followers skip all of this — they trust the lead cleared the way.
- **Arrival detection**: `ziel_erreicht()` (schedule entry reached) is triggered from the lead's `hop_check()`.
- **Cost accounting**: running costs are booked once per tile, from the lead's `hop()` (`vehicle.cc:581`), regardless of convoy length.
- **Minimap**: the lead updates the minimap pixel on `enter_tile`; the tail updates it on `leave_tile`.

### How all vehicles move together

Movement is driven by **`sync_step(delta_t)`** (`simconvoi.cc:890`), called every world-clock tick:

1. `calc_acceleration(delta_t)` computes the convoy's single shared speed `akt_speed`
2. `sp_soll += akt_speed * delta_t` — distance to cover this tick
3. Lead moves first: `sp_hat = fahr[0]->do_drive(sp_soll)`
4. All followers move the **exact same distance `sp_hat`**:
   ```
   for i = 1..vehicle_count-1:
       fahr[i]->do_drive(sp_hat)
   ```

`sp_hat` may be less than `sp_soll` if the lead was blocked. Followers always mirror the lead's actual travel — there is no chain-tension simulation.

**`step()`** (`simconvoi.cc:1190`) is a separate, lower-frequency call that handles the state machine (schedule changes, route recalculation, loading) but does **not** move vehicles.

### How each vehicle tracks its position

Each `vehicle_t` holds:
- `pos` — the tile it currently occupies
- `pos_next` — the tile it is heading toward
- `steps` / `steps_next` — sub-tile position (0–255 within the current tile)
- `route_index` — its own index into the convoy's shared `route_t`

When enough steps accumulate to cross a tile boundary, `hop()` fires: the vehicle calls `leave_tile()`, updates `pos = pos_next`, increments `route_index`, sets `pos_next = route->at(route_index)`, and calls `enter_tile()`.

Followers are kept spaced apart not by any "follow distance" logic but because they each start offset along the route by their physical lengths (via `initialise_journey()` at depot start). From then on, they all receive the same `sp_hat` each tick and independently increment their own `route_index` as they cross tile boundaries.

### Speed: minimum of all vehicles

`min_top_speed` (`simconvoi.h:144`) is the **minimum top speed across all vehicles** (`calc_min_top_speed()`, `simconvoi.cc:99`). A fast engine pulling a slow wagon is limited by the wagon. Actual speed is further constrained by the current track speed limit and physics (power, weight, gradient) in `calc_acceleration()` (`simconvoi.cc:716`).

### Cargo capacity: sum across all vehicles

Each vehicle carries its own cargo list (`fracht`, a `slist_tpl<ware_t>`). The convoy's `loading_level` is computed in `calc_loading()` (`simconvoi.cc:3171`) by summing `get_cargo_max()` and `get_total_cargo()` across all vehicles, then computing a percentage.

### What `convoi_t` owns vs. what `vehicle_t` owns

| Owned by `convoi_t` | Owned by `vehicle_t` |
|---|---|
| `fahr[]` array | `pos`, `pos_next` (tile position) |
| `route` (the shared path) | `steps`, `steps_next` (sub-tile position) |
| `schedule` | `route_index` (this vehicle's place in the shared route) |
| `akt_speed`, `sp_soll` (shared speed) | `leading`, `last` flags |
| `min_top_speed` | `desc` (immutable type descriptor) |
| `sum_power`, `sum_weight`, etc. | `fracht` (this vehicle's cargo list) |
| `state` (state machine) | `cnv` (back-pointer to convoy) |
| `loading_level`, `loading_limit` | `speed_limit` (this tile's speed limit) |
| `line`, `home_depot` | `sum_weight` (this vehicle's weight) |
| `financial_history[][]` | `target_halt` (cargo routing) |

A `vehicle_t` alone cannot move. It has no route and no schedule — only its position, its cargo, and a pointer back to its convoy.

### Formation at depot: how a convoy is assembled and started

1. **`depot_t::add_convoi()`** (`depot.cc:257`): creates a new empty `convoi_t(owner)`. State = `INITIAL`.
2. **`depot_t::append_vehicle()`** (`depot.cc:202`): calls `cnv->add_vehicle(veh, infront)` for each vehicle the player adds. Aggregates are updated after each addition.
3. **Assign a schedule**: `convoi_t::create_schedule()` (`simconvoi.cc:1909`) calls `fahr[0]->generate_new_schedule()` to create the right schedule type for the vehicle's waytype.
4. **`depot_t::start_convoi()`** (`depot.cc:408`): validates the convoy (has power, vehicles are compatible, route exists), then calls `cnv->start()`.
5. **`convoi_t::start()`** (`simconvoi.cc:1537`): sets `leading`/`last` flags, adds convoy to the world's sync list, transitions state to `ROUTING_1`.
6. **`convoi_t::vorfahren()`** (`simconvoi.cc:2061`): places all vehicles on the start tile, then drives them `train_length` worth of steps so they are staggered correctly on the track before the convoy enters `CAN_START` → `DRIVING`.

---

## Part 2: Route & Schedule Planning

### Data Model

#### Core structures

| File | Structure | Role |
|---|---|---|
| `dataobj/schedule_entry.h` | `schedule_entry_t` | One stop or waypoint |
| `dataobj/schedule.h` | `schedule_t` (abstract) | Ordered list of entries + current position |
| `simline.h` | `simline_t` | Named line: wraps a `schedule_t` + list of assigned convoys |

**`schedule_entry_t`** holds:
- `koord3d pos` — tile coordinate
- `uint8 minimum_loading` — 0–100: wait until this fill %; 101–255: absolute departure mode
- `uint16 waiting_time` — ticks to wait before departing

**`schedule_t`** holds:
- `minivec_tpl<schedule_entry_t> entries` — the ordered stop list
- `uint8 current_stop` — index of the next target

There are waytype-specific subclasses: `train_schedule_t`, `truck_schedule_t`, `ship_schedule_t`, etc.

#### Stops vs. waypoints

Not stored in the entry itself — computed at runtime by `convoi_t::is_waypoint()` (`simconvoi.cc:235`). A tile is a **stop** if it resolves to a `halthandle_t` owned by the convoy's player. Otherwise it's a **waypoint**. Depots are a third category.

#### Cyclic vs. round-trip

There is **no run-mode flag**. Both modes are expressed purely by the shape of the `entries` list:
- **Cyclic**: entries form a natural loop (e.g. `[A, B, C]` wraps back to A)
- **Round-trip**: `add_return_way()` (`schedule.cc:482`) bakes the return path into the list (e.g. `[A, B, C]` becomes `[A, B, C, B]`), then the vehicle advances cyclically through it

The GUI "return ticket / invert stops" button triggers `add_return_way()`.

### Schedule / Stop Logic

#### Advancing stops

`schedule_t::advance()` (`schedule.h:111`):

```
current_stop = (current_stop + 1) % entries.get_count()
```

Called from:
- `convoi_t::ziel_erreicht()` (`simconvoi.cc:1650`) — waypoint tile reached
- `convoi_t::hat_gehalten()` (`simconvoi.cc:3149`) — loading finished at a stop
- `vehicle_t::hop()` (`vehicle.cc:542`) — mid-route waypoint crossing

The absolute-departure variant skips over grouped entries at the same position before incrementing.

### Vehicle Assignment

#### Assigning to a line

`convoi_t::set_line(linehandle_t)` (`simconvoi.cc:3389`) stores the line, then `check_pending_updates()` (`simconvoi.cc:3449`):
1. Picks the best matching `current_stop` by comparing the old position with the new schedule
2. Calls `line->add_convoy(self)` to register the convoy
3. Copies the line's schedule into the convoy's own private `schedule` copy

Each convoy holds its **own copy** of the schedule so its `current_stop` is independent.

#### Unassigning from a line

`convoi_t::unset_line()` (`simconvoi.cc:3416`) calls `line->remove_convoy(self)` and clears the handle. The convoy keeps its schedule copy and keeps running.

#### Returning to depot

Two mechanisms:

1. **`withdraw` flag** (`simconvoi.h:112`): set via the GUI or `'w'` tool command. While set, the convoy refuses new cargo. When `loading_level == 0` at a stop, it calls `self_destruct()` — the convoy vanishes and its vehicles return to the depot's vehicle pool.

2. **`send_to_depot()`** (`simconvoi.cc:3964`): searches `depot_t::get_depot_list()` for the nearest compatible depot, pathfinds there, **prepends the depot tile to the schedule**, and drives normally. On arrival, `betrete_depot()` fires and state returns to `INITIAL`.

The convoy's **home depot** (`convoi_t::home_depot`, `simconvoi.h:251`) is set when it first starts and updated on each depot entry.

### Jump-to-Stop

There is **no teleportation**. The mechanism is:

1. Set `schedule->set_current_stop(n)` to the new index
2. Set state to `ROUTING_1` or `EDIT_SCHEDULE`
3. `drive_to()` pathfinds from the convoy's current physical position to the new target

This is triggered via **`tool_change_convoi_t`** (`simtool.cc:7776`) with command code `'l'` (change line) or `'g'` (change schedule). The convoy always drives to the new stop — it does not skip tiles.

### Pathfinding Integration

#### When

Pathfinding happens **at departure time** (not at assignment). It is triggered whenever the convoy enters state `ROUTING_1`, which calls `convoi_t::drive_to()` (`simconvoi.cc:1020`).

#### How

`drive_to()` calls `fahr[0]->calc_route(start, target, speed, &route)` → `route_t::calc_route()` → `intern_calc_route()` (`route.cc:291`): an **A\* search** using a binary heap. Water routes use **Jump Point Search (JPS)** instead.

#### Waypoint chaining

When the next entry is a waypoint, `drive_to()` chains route segments in a loop — pathfinding waypoint→waypoint until it reaches a real stop — and concatenates all tile lists into one route. The `current_stop` is then reset to the pre-chain index; actual waypoint advancement happens mid-drive via `vehicle_t::hop()`.

#### Route caching

The computed route (`convoi_t::route`) persists until:
- The route end is reached
- A blocked tile triggers re-routing (`state = ROUTING_1`)
- The schedule changes

### State Machine

#### States (`simconvoi.h:55`)

| State | Meaning |
|---|---|
| `INITIAL` (0) | In depot, not on map |
| `EDIT_SCHEDULE` (1) | Schedule window open or just changed |
| `ROUTING_1` (2) | Needs pathfinding |
| `NO_ROUTE` (5) | Pathfinding failed, retrying every ~25s |
| `DRIVING` (6) | Moving along route |
| `LOADING` (7) | At a halt, loading/unloading |
| `WAITING_FOR_CLEARANCE` (8/9/13) | Blocked at signal/junction (escalating monthly) |
| `CAN_START` (10/11/14) | Ready to depart from depot (escalating monthly) |
| `LEAVING_DEPOT` (15) | Exiting depot, vehicles not fully on track yet |
| `SELF_DESTRUCT` (12) | Scheduled for removal |

#### Key transitions

```
INITIAL ──[start()]──► ROUTING_1 ──[drive_to() ok]──► CAN_START / DRIVING
                                 ──[drive_to() fails]► NO_ROUTE

DRIVING ──[halt reached]──────────────────────────► LOADING
        ──[waypoint reached]──[advance(); drive_to()]► ROUTING_1
        ──[depot reached]─────────────────────────► INITIAL
        ──[tile blocked]──────────────────────────► WAITING_FOR_CLEARANCE

LOADING ──[load complete / time expired]──[advance()]► ROUTING_1
        ──[withdraw && empty]─────────────────────► SELF_DESTRUCT

EDIT_SCHEDULE ──[window closes, not at stop]──► ROUTING_1
              ──[window closes, already at halt]► LOADING

WAITING_FOR_CLEARANCE ──[tile clears]──► DRIVING
                      ──[1 month]───────► WAITING_FOR_CLEARANCE_ONE_MONTH
                      ──[2 months]──────► WAITING_FOR_CLEARANCE_TWO_MONTHS
```

#### Loading exit conditions (`hat_gehalten()`, `simconvoi.cc:3085`)

- `loading_level >= minimum_loading` (fill % reached)
- `waiting_time` elapsed
- Absolute departure slot triggered (`minimum_loading > 100`)

---

## Braking at Stops vs. Full Speed Through Waypoints

### The short answer

The tile itself carries no speed information about being a stop or waypoint. Braking is controlled entirely by a **route-index countdown** in `calc_acceleration()`. That countdown only activates when `next_stop_index` is set close to the end of the route — and `next_stop_index` is only set close when the route ends at a **halt**. For a waypoint, `next_stop_index` is left at a huge sentinel value (65531), so the countdown never fires and the convoy never brakes.

---

### `next_stop_index` — the halt/waypoint decision

`convoi_t::set_next_stop_index()` (`simconvoi.cc:3617`) is where the decision is made:

```cpp
void convoi_t::set_next_stop_index(route_t::index_t n)
{
    if (n == INVALID_INDEX) {          // no signal found — check route end
        grund_t *gr = welt->lookup(route.back());
        if (gr && gr->is_halt()) {
            n = route.get_count() - 1 - 1;   // brake one tile before the halt
        }
        // if it's a waypoint: n stays == INVALID_INDEX (0xFFFA)
    }
    next_stop_index = n + 1;
    // halt  → next_stop_index = route.get_count() - 1  (close to end)
    // waypoint → next_stop_index = 0xFFFA + 1 = 65531  (enormous)
}
```

Called from `rail_vehicle_t::can_enter_tile()` after signal reservation. The `INVALID_INDEX` sentinel (`route.h:30`) means "no signal found — check if the destination is a stop."

---

### The brake countdown in `calc_acceleration()`

Every `sync_step` tick, `calc_acceleration()` (`simconvoi.cc:716`) recomputes the target speed `akt_speed_soll`. The braking section (`simconvoi.cc:753–768`):

```cpp
uint32 tiles_left = 1 + next_stop_index - fahr[0]->get_route_index();

if (tiles_left < 4) {
    static sint32 brake_speed_countdown[4] = {
        kmh_to_speed(25),   // 0 tiles left
        kmh_to_speed(50),   // 1 tile left
        kmh_to_speed(100),  // 2 tiles left
        kmh_to_speed(200),  // 3 tiles left
    };
    akt_speed_soll = min(akt_speed_soll, brake_speed_countdown[tiles_left]);
}
```

| `next_stop_index` value | `tiles_left` | Braking? |
|---|---|---|
| Set to `route.get_count()-1` (halt) | 3, 2, 1, 0 as convoy approaches | Yes — 200 → 100 → 50 → 25 km/h cap |
| 65531 (waypoint sentinel) | Always enormous | No — `tiles_left < 4` never true |

The tile's own infrastructure speed limit (`weg->get_max_speed()`) is applied separately and identically for both halts and waypoints — it has nothing to do with stopping.

---

### `check_for_finish` — arriving at the route end

`vehicle_t::check_for_finish` (`vehicle.h:79`) is a 1-bit flag set in `hop()` (`vehicle.cc:528`) when the lead vehicle's `route_index` runs off the end of the route array:

```cpp
if (route_index < route.get_count() - 1) {
    route_index++;
    pos_next = route.at(route_index);
} else {
    route_index++;
    check_for_finish = true;     // no more tiles ahead
}
```

When `check_for_finish` is true, the next call to `hop_check()` (`vehicle.cc:414`) calls `cnv->ziel_erreicht()` and returns `NULL`. That `NULL` exits the `do_drive()` hop loop immediately — the vehicle stops advancing.

---

### `ziel_erreicht()` — what happens at each destination type

`convoi_t::ziel_erreicht()` (`simconvoi.cc:1613`):

```cpp
// halt path:
akt_speed = 0;          // hard stop
state = LOADING;        // begin loading/unloading

// waypoint path:
// akt_speed is NOT touched
schedule->advance();    // silently move to next schedule entry
state = ROUTING_1;      // find next route immediately
```

At a halt the convoy is **zeroed** and enters `LOADING`. Because the brake countdown already reduced speed to ≤25 km/h before this point, the physical jolt is small.

At a waypoint speed is untouched. The convoy is physically blocked for one async `step()` cycle (the time needed to compute the next route), then resumes at whatever speed it had.

---

### The faster path: waypoints pre-planned into a single route

When `drive_to()` builds a route (`simconvoi.cc:1085–1139`), it detects consecutive waypoints and **chains them into one single route** without stopping:

```cpp
while (is_waypoint(ziel)) {
    schedule->advance();
    next_ziel = schedule->get_current_entry().pos;
    // pathfind start→ziel, append to route
    // set schedule_target = ziel (the waypoint pos)
    ziel = next_ziel;
}
// final destination is a halt — route ends there
```

In this case the convoy's route array goes all the way from the departure stop through all intermediate waypoints to the next halt in one shot. The vehicle never needs to stop and re-route at all. As it passes through each waypoint tile, `hop()` (`vehicle.cc:536`) detects `get_pos() == schedule_target` and silently advances the schedule:

```cpp
if (get_pos() == cnv->get_schedule_target()) {
    cnv->get_schedule()->advance();
    next_target = cnv->get_schedule()->get_current_entry().pos;
    cnv->set_schedule_target(is_waypoint(next_target) ? next_target : koord3d::invalid);
}
```

This is a **zero-cost passthrough** — no stop, no state change, no speed change. The schedule just ticks forward invisibly while the vehicle flies through at full speed.

`check_for_finish` is only set when the route array itself runs out — i.e., at the final halt at the end of the chained route. All the waypoints in between are passed silently via `schedule_target` comparison in `hop()`.

---

### Complete flow: stop vs. waypoint

**Convoy approaching a halt (e.g. rail):**
```
can_enter_tile() → set_next_stop_index(INVALID_INDEX)
                 → gr->is_halt() true
                 → next_stop_index = route.get_count() - 1

Each hop() → must_recalc_data_front() → triggers brake recalc

calc_acceleration(): tiles_left shrinks to 3→2→1→0
    speed capped at 200→100→50→25 km/h

Last hop() → check_for_finish = true
           → steps_next halved for N/W direction (centers train on halt tile)

hop_check() → ziel_erreicht() → akt_speed = 0, state = LOADING
```

**Convoy passing through a waypoint (pre-planned route):**
```
drive_to() chains waypoint into route, sets schedule_target = waypoint_pos
next_stop_index = 65531 (no braking ever)

hop() at waypoint tile:
    get_pos() == schedule_target → schedule->advance() silently
    speed untouched, route_index continues incrementing

No check_for_finish until the final halt tile at route end
```

**Convoy whose route ends at a waypoint (separate route segment):**
```
set_next_stop_index(INVALID_INDEX) → gr->is_halt() false → n stays INVALID_INDEX
next_stop_index = 65531 → no braking

check_for_finish set at route end
hop_check() → ziel_erreicht()
    akt_speed NOT zeroed
    schedule->advance(), state = ROUTING_1
    vehicle pauses one step() cycle, then resumes at full speed
```

---

### Key line numbers

| Concept | File | Lines |
|---|---|---|
| `set_next_stop_index()` — halt vs waypoint decision | `simconvoi.cc` | 3617–3628 |
| `INVALID_INDEX` constant | `dataobj/route.h` | 30 |
| `calc_acceleration()` brake countdown | `simconvoi.cc` | 753–768 |
| `brake_speed_countdown[]` array | `simconvoi.cc` | 758–764 |
| `check_for_finish` flag declaration | `vehicle/vehicle.h` | 79 |
| `check_for_finish` set in `hop()` | `vehicle/vehicle.cc` | 528–531 |
| `hop_check()` → `ziel_erreicht()` on finish | `vehicle/vehicle.cc` | 414–423 |
| `ziel_erreicht()` — halt: `akt_speed=0`, LOADING | `simconvoi.cc` | 1638–1646 |
| `ziel_erreicht()` — waypoint: no speed change, ROUTING_1 | `simconvoi.cc` | 1649–1652 |
| `hop()` — silent waypoint advance via `schedule_target` | `vehicle/vehicle.cc` | 536–545 |
| `hop()` — `must_recalc_data_front()` after every hop | `vehicle/vehicle.cc` | 586 |
| `hop()` — halved `steps_next` for N/W at halt | `vehicle/vehicle.cc` | 582–583 |
| Waypoint pre-planning chain loop | `simconvoi.cc` | 1085–1139 |
| `next_stop_index` initialised to 65535 | `simconvoi.cc` | 161 |

---

## Schedule Changes, Path Invalidation, and Spacing

### When a schedule is edited — how the convoy syncs

The edit flow:
1. Player closes the schedule editor → `convoi_info_t::apply_schedule()` (`convoi_info.cc:261`) fires command `'g'` → `convoi_t::set_schedule()` (`simconvoi.cc:1850`)
2. `set_schedule()` replaces the schedule pointer, then **unconditionally forces `state = EDIT_SCHEDULE`** (`simconvoi.cc:1900`) — even if the convoy is currently `DRIVING`. The convoy does not stop mid-tile; it just picks up the new state on the next `step()` tick.
3. The following `step()` tick sees `EDIT_SCHEDULE`, calls `set_schedule()` again (line 1221), and transitions to `ROUTING_1` — triggering a full re-route from the convoy's current physical position.

There is no teleport. The convoy continues to coast physically while the state machine catches up.

**`check_pending_updates()` best-match algorithm** (`simconvoi.cc:3449`):
When reconciling the convoy's `current_stop` with a changed schedule, it reads the old next-4-entry sequence and scores every entry in the new schedule by how many of those 4 entries it shares with (`matches_halt()`), weighted 3×current + 4×next + 2×nextnext + 1×nextnextnext. The highest-scoring entry wins. If nothing matches at all, it falls back to the line's own `current_stop`.

---

### If the current target stop is removed

`schedule_t` protects itself with `make_current_stop_valid()` (`schedule.h:36`), called after every mutation:

```cpp
if (current_stop >= entries.get_count())
    current_stop = count - 1;   // clamp to last entry
if (count == 0)
    current_stop = 0;           // empty schedule
```

More specifically, `remove_entry(N)` (`schedule.cc:193`):
- If `current_stop > N`: decrements `current_stop` first (preserves the convoy's relative target)
- If `current_stop == N`: does not decrement — after removal, `current_stop` now points to what was the next stop (the successor slides into the same index)
- Then calls `make_current_stop_valid()` to clamp if the deleted stop was the last one

Net result: the convoy silently redirects to the stop after the deleted one, or to the new last stop if the deleted stop was last. It never crashes or stalls due to a bad index. If the schedule shrinks to a single entry, `make_valid()` clears it entirely and the convoy transitions to `NO_ROUTE`.

---

### If the current target loses its viable path

The convoy does not know in advance. It finds out **when `drive_to()` runs** (`simconvoi.cc:1020`):

```
ROUTING_1 → drive_to() → calc_route() → A* fails → state = NO_ROUTE, wait_lock = 25,000 ticks
```

`NO_ROUTE` retries `drive_to()` every **25,000 ticks (~25 real-time seconds)**. Every in-game month it re-reports the problem to the player but stays in `NO_ROUTE` indefinitely. There is no exponential back-off and no automatic rerouting via an alternative stop — the convoy just keeps retrying the same target until the path becomes available again or the player intervenes.

---

### If the track/road under a moving vehicle is deleted

**Prevention first**: `vehicle_t::get_removal_error()` (`vehicle.cc:1109`) always returns a non-null error string. `tool_wayremover_t::calc_route()` (`simtool.cc:3608`) calls `obj->get_removal_error()` on every object on every tile in the removal path — if any vehicle is present, the demolition is blocked. A player cannot delete a tile a convoy is physically standing on.

**Detection for the next tile**: Every step, `vehicle_t::hop_check()` (`vehicle.cc:426`) checks the tile the vehicle is about to enter:

```cpp
grund_t *bd = welt->lookup(pos_next);
if (bd == NULL || !check_next_tile(bd) || cnv->get_route()->empty()) {
    cnv->suche_neue_route();   // → state = ROUTING_1
    return NULL;
}
```

`check_next_tile()` returns false if the required track/road type no longer exists on the tile (e.g. the rail was removed between two ticks). This triggers:

```
suche_neue_route() → ROUTING_1 → drive_to() → calc_route() fails → NO_ROUTE
```

So the convoy stops at its current tile, attempts to reroute, and if the route is now broken it enters `NO_ROUTE` and retries every 25 s.

---

### How a line schedule update is synced to all convoys

The chain when a player edits a **line's** schedule (affects all convoys on that line):

1. `simlinemgmt_t::update_line(line)` (`simlinemgmt.cc:54`) iterates every convoy on the line and calls `cnv->set_update_line(line)` — this sets `line_update_pending = line` on each convoy.
2. Convoys **in depot** get `check_pending_updates()` called immediately (same function, line 62).
3. Convoys **on the road** pick it up lazily: the very first line of `convoi_t::step()` (line 1196) is:
   ```cpp
   if (line_update_pending.is_bound()) { check_pending_updates(); }
   ```
4. `check_pending_updates()` runs the best-match algorithm (described above), copies the new schedule into the convoy, and forces `state = EDIT_SCHEDULE` if the destination changed.

Key point: `simline_t` does **not push** the new schedule directly to convoys. It marks each convoy with `line_update_pending` and each convoy pulls the update on its own next `step()` tick. The update is thus **eventually consistent** — all convoys on the line update within one async step cycle, but not all simultaneously.

---

### Headway and spacing between convoys

**There is no headway enforcement in Simutrans.**

Convoys on the same line operate entirely independently. There is no "wait for gap" state, no minimum separation, no timetable-based departure slots across convoys (only per-entry absolute-departure times). Bunching is possible and common.

The only indirect effects:

| Mechanism | Where | Effect |
|---|---|---|
| `halt_served_this_step` | `hat_gehalten()` `simconvoi.cc:3000` | A trailing convoy may find nothing left to load at a stop a leading convoy just cleared — causing it to depart immediately (zero loading wait). This makes bunching self-reinforcing, not self-correcting. |
| Block signals | Infrastructure (signal objects on track) | Prevent two trains entering the same track block simultaneously. This enforces minimum separation on rail but is infrastructure-driven, not schedule-driven. |
| Same-line notification suppression | `new_month()` `simconvoi.cc:1454` | Suppresses the "vehicle blocked" player alert if another convoy on the same line is loading ahead (assumed to be causing the blockage). No operational effect. |

**Summary**: spacing between convoys on the same line in Simutrans is determined entirely by track signals (for rail) and first-come-first-served tile occupancy (for road). The schedule system has no awareness of other convoys on the same line.

---

## German → English Glossary

Simutrans originated as a German-language project. Most of the core codebase uses German identifiers. Here is a translation of every German name that appears in this document, plus common ones you will encounter while reading the source.

### Variables and Fields

| German identifier | Literal translation | What it means in context |
|---|---|---|
| `fahr[]` | *fahren* = to drive / travel | The array of vehicles in a convoy |
| `fracht` | freight / cargo | A vehicle's cargo list (`slist_tpl<ware_t>`) |
| `fracht_max` | maximum freight | Total cargo capacity of the convoy |
| `fracht_menge` | freight quantity / amount | Current total cargo units loaded |
| `akt_speed` | *aktuelle* Geschwindigkeit = current speed | The convoy's live speed this tick |
| `sp_soll` | *Soll* = target / setpoint | Distance the convoy *should* travel this tick |
| `sp_hat` | *hat* = has (past tense) | Distance the convoy *actually* travelled this tick |
| `welt` | world | The global simulation world object (`karte_t`) |
| `koord` / `koord3d` | *Koordinate* = coordinate | 2-D or 3-D tile coordinate |
| `bd` | *Boden* = ground / floor | A ground tile (`grund_t`) |
| `weg` | way / road / track | A track or road object on a tile |
| `v_nr` | *Fahrzeug-Nummer* = vehicle number | Index of the current vehicle during depot-exit loop |
| `sum_gesamtweight` | *Gesamt* = total, *Gewicht* = weight | Total weight of convoy including cargo |
| `ware` / `ware_t` | goods / merchandise | A cargo item (one unit of a good type) |
| `cnv` | abbreviation of *Konvoi* | Pointer to the convoy (`convoi_t*`) |

### Functions and Methods

| German identifier | Literal translation | What it does |
|---|---|---|
| `ziel_erreicht()` | *Ziel* = goal/destination, *erreicht* = reached | Called when the convoy arrives at the next schedule entry |
| `hat_gehalten()` | *hat gehalten* = has stopped / has halted | Called each step while loading; decides when to depart |
| `betrete_depot()` | *betrete* = enter (imperative), *Depot* = depot | Handles a convoy physically entering a depot |
| `vorfahren()` | drive forward / pull forward | Places the convoy on the track and advances it to the start position after route calculation |
| `laden()` | to load | Performs the per-step cargo loading/unloading at a halt |
| `suche_neue_route()` | search new route | Triggers a re-route (sets state to `ROUTING_1`) |
| `warten_bis_weg_frei()` | wait until the way is clear | Called when the lead vehicle cannot enter the next tile; transitions to `WAITING_FOR_CLEARANCE` |
| `set_erstes_letztes()` | set first–last | Refreshes the `leading` / `last` flags on `fahr[0]` and `fahr[vehicle_count-1]` |
| `pruefe_alle()` | check all / verify all | Validates that all vehicles in the convoy are compatible |
| `add_return_way()` | add return way | Appends or mirrors the stop list to create a round-trip schedule |
| `intern_calc_route()` | internal calculate route | The private A* implementation inside `route_t` |
| `hop()` | hop (English, but German-context use) | Moves a vehicle one tile forward along the route |
| `hop_check()` | hop check | Pre-checks whether the next tile can be entered (signals, blocking) |

### Class and File Names

| German identifier | Literal translation | What it is |
|---|---|---|
| `simconvoi` | *Simulation Konvoi* | Convoy class and logic (`convoi_t`) |
| `simline` | *Simulation Linie* | Named line class (`simline_t`) |
| `simhalt` | *Simulation Halt* | Station/stop class (`haltestelle_t`) |
| `haltestelle_t` | *Haltestelle* = bus stop / station | The stop object (platform, station) |
| `fahrplan_t` | *Fahrplan* = timetable / schedule | Old name for `schedule_t`; still appears in some older files |
| `simtool` | *Simulation Werkzeug* (tool) | Player action tools (build, demolish, assign convoy…) |
| `karte_t` | *Karte* = map | The world/map object (accessible via `welt`) |
| `grund_t` | *Grund* = ground / tile | A single map tile |
| `ding_t` / `obj_t` | *Ding* = thing / object | Base class for all objects placed on a tile |
| `minivec_tpl` | *Mini-Vektor* = small vector | A compact resizable array template |

---

## Key Files Quick Reference

| File | What's in it |
|---|---|
| `dataobj/schedule_entry.h` | `schedule_entry_t` struct |
| `dataobj/schedule.h/.cc` | `schedule_t`, `advance()`, `add_return_way()` |
| `simline.h/.cc` | `simline_t`, convoy list management |
| `simconvoi.h/.cc` | Full state machine, convoy aggregates, `drive_to()`, `sync_step()`, `set_line()`, `send_to_depot()` |
| `dataobj/route.h/.cc` | A* / JPS pathfinder |
| `vehicle/vehicle.h/.cc` | `vehicle_t`: per-vehicle state, `hop()`, `hop_check()`, `do_drive()` |
| `tool/simtool.cc` | `tool_change_convoi_t`: network-safe commands `'g'/'l'/'w'` |
| `gui/components/gui_schedule.cc` | Schedule editing GUI, return-trip button |
| `obj/depot.cc` | Depot assembly (`add_convoi`, `append_vehicle`, `start_convoi`), `betrete_depot()` |
