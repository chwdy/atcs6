# UI Inspector — Design Document

Branch: `add-design-docs` (target runtime branch: `attempt-0504`)
Status: design only — no code changes proposed in this doc
Author audience: a frontend/backend engineer pair who will implement

## Problem

The user debugs by describing UI symptoms. They cannot read all the AI-generated code, and the UI today shows almost no live entity state. So every bug becomes "the cab arrow looks wrong" instead of "controller.travel_direction = E, design says it should be W."

The Inspector exposes every runtime entity's state — stored fields, derived `@property` values, latches, references — directly in the UI, so the user can compare the actual runtime to `design_new.md` field-by-field.

A generic dotted-path debug inspector already exists at `/api/debug/inspect` (`debug_inspector.py:258-275`) and `/debug` (static page). It dumps raw Python objects and has no concept of derived properties, latches, or cross-entity references. The Inspector described below is a typed, entity-aware layer on top of (or replacing) it for the entities the user actually cares about.

---

## 1. Entity inventory

Each entity below is grouped by lifecycle and lists its **inspectable surface** as it exists in the post-migration code. Each row is a single field, derived property, latch, or reference. Field source is the file:line where it lives.

### 1.1 Singleton runtime

#### `SimulationController` (`simulation.py:47-401`)

- `network: RailwayNetwork` — reference (singleton)
- `path_dispatcher: Optional[PathDispatcher]` — reference (singleton)
- `last_update_time: datetime` — stored
- `controllers: Dict[str, TrainController]` — registry; per-train links
- `physics_instances: Dict[str, TrainPhysics]` — registry; per-train links
- `power_controller: BlockPowerController` — reference (singleton)
- `route_executor: RouteExecutor` — reference (singleton)
- `speed_profile_registry: SpeedProfileRegistry` — reference (singleton)
- `simulation_time: float` — stored

Useful derived info to expose:
- Number of registered controllers/physics
- Last tick wall-clock duration (not currently stored — would need a moving log; out of scope for v1)

#### `PathDispatcher` (`path_dispatcher.py:62-2237`)

- `network: RailwayNetwork` — reference
- `active_paths: Dict[str, Path]` — registry (every path the dispatcher knows about, regardless of status)
- `paths_by_id: Dict[str, Path]` — `@property` alias for `active_paths` (`path_dispatcher.py:80-88`)
- `path_sequence_counter: int` — stored
- `completed_path_events: List[Dict[str, Any]]` — stored history (drained by `get_and_clear_completion_events()`, `path_dispatcher.py:1046`)

Derived info to expose:
- Path counts per status (PENDING / SETTING / SET / ACTIVE / CLEARING / COMPLETED / CANCELLED)
- For each active train, the dispatcher's currently active path id (`active_path_id_by_train(train_id)`)

#### `RouteExecutor` (`route_executor.py:117-`)

- `network: RailwayNetwork` — reference
- `path_dispatcher: Optional[PathDispatcher]` — reference
- `execution_states: Dict[str, RouteExecutionState]` — registry of per-train RouteRuns
- `controllers: Dict[str, TrainController]` — shared with `SimulationController.controllers`
- `_owns_controllers: bool` — internal flag

Derived info:
- For each train: `_get_active_path_id(train_id)` — what the dispatcher reports as active
- `order_runnable_trains(...)` — current ordering used in the tick loop; can be exposed read-only

#### `BlockManager` (`block_manager.py:46-`)

- `blocks: Dict[str, Block]` — backing registry (shared with `RailwayNetwork.blocks`)
- `history: deque` — recent block state transitions (already exposed at `/api/debug/block-history`, `debug_inspector.py:296-317`)

#### NotificationCenter — **does not exist**

The design doc references a `NotificationCenter`. There is no such class in the post-migration code. The only notification surface is `TrainController.pending_notifications: List[str]` (`train_controller.py:314`). This is a per-train queue, not a singleton. See discrepancies below.

---

### 1.2 Per-train

#### `Train` (`train.py:349-`)

Stored fields:
- `train_id: str`
- `consist: List[TrainCar]`
- `max_speed: float`, `current_speed: float` (km/h), `acceleration: float` (m/s²), `priority: int`
- `max_braking_rate: float`, `is_emergency_braking: bool`
- `energy_consumed: float`, `efficiency: float`
- `position: Optional[TrainPosition]` — composite, see below
- `schedule: Optional[TrainSchedule]`
- `current_path: Optional[List[Tuple[int, int, Direction]]]` — **cached path snapshot** (not source of truth; see discrepancies)
- `path_index: int`
- `reserved_blocks: List[str]`
- `assigned_path_id: Optional[str]` — Train-side stored field (also exposed as a property on `TrainController`)
- `model: str`, `car_number: str`, `min_radius_cm: float`
- `active_cab: Optional["A"|"B"]` — Train-side mirror; also stored on `TrainController.active_cab`
- `travel_direction: "forward"|"reverse"` — Train-side stored field; **note**: `TrainController.travel_direction` is derived
- `state: TrainState`, `wait_reason: Optional[str]`
- `speed_profile_id: Optional[str]`, `assigned_route_id: Optional[str]`
- `manual_target_speed: float`, `emergency_brake: bool`
- `ato_running: bool`, `ato_destination_block_id: Optional[str]` — Train-side mirrors

Derived `@property`:
- `length` (m), `length_cm` — derived from `consist` (`train.py:470-481`)
- `braking_distance` — derived from speed and acceleration (`train.py:484-493`)
- `facing_direction` — derived from `position` (`train.py:495-`)

References:
- `assigned_path_id` → Path
- `assigned_route_id` → Route
- `speed_profile_id` → SpeedProfile
- `position.occupied_blocks[]` → Block list
- `reserved_blocks[]` → Block list
- `current_path[]` → tile coordinates only

#### `TrainPosition` (`train.py:21-`)

- `end_a_tile`, `end_a_distance_cm`, `end_a_entry_edge`
- `end_b_tile`, `end_b_distance_cm`, `end_b_entry_edge`
- `leading_end: Optional[LeadingEnd]`
- `occupied_tiles: List[Tuple[int, int]]`
- `occupied_blocks: List[str]`

Derived `@property`:
- `head_tile`, `head_distance_cm`, `head_entry_edge` (`train.py:62-83`)
- `tail_tile`, `tail_distance_cm`, `tail_entry_edge`
- `facing_direction: Optional[Direction]` (computed from leading_end + tile geometry; `train.py:108-186`)

#### `TrainController` (`train_controller.py:276-4123`)

This is the largest entity. All fields are listed because the user's debugging often hinges on them.

Stored fields (live runtime state):
- `train_id: str`, `network`, `path_dispatcher` — references
- Power & safety: `power_on`, `eb_active`, `atc_warning_active`
- `pending_notifications: List[str]`
- Cab & mode: `active_cab`, `control_mode` ("ato"|"manual"), `manual_mode_type` ("simulation"|"power_percent")
- Operational: `state: TrainState`, `handle_position`, `manual_power_percent`
- ATO: `ato_running: bool`, `path_backed_run_active: bool`
- ATC scratch: `clearance_ahead_cm`, `atc_speed_limit`, `current_block_speed_limit`
- Configuration: `safety_margin_cm`, `signal_lookahead_blocks`
- ATO info: `ato_info: Optional[ATOInfo]`
- `_previous_speed: float`, `handle_reason: str`
- `_last_speed_limit_reason: Optional[str]` — internal cache for the `speed_limit_reason` property

Phase-3 mailbox (latches):
- `queued_progress_reports: List[ControllerProgressReport]`
- `pending_path_cancel_request: Optional[PendingPathCancelRequest]`
- `pending_route_abort: Optional[PendingRouteAbortRequest]`
- `pending_power_off: bool`
- `pending_route_finished: bool`
- `pending_route_deactivate: Optional[PendingRouteDeactivateRequest]`

Controller-owned live span/speed:
- `current_span: Optional[Any]` — TrainSpan (writes from `on_physics_updated`)
- `current_speed_kmh: float` — live speed

Derived `@property` (read-only, recomputed every read):
- `received_path_plan: Optional[Path]` — calls `path_dispatcher.get_active_path(train_id)` (`train_controller.py:417-431`)
- `assigned_path_id: Optional[str]` — `received_path_plan.path_id` (`train_controller.py:433-439`)
- `ato_path_id: Optional[str]` — alias of `assigned_path_id` (`train_controller.py:441-444`)
- `ato_destination_block_id: Optional[str]` — `received_path_plan.authority_end_block_id` with `end_block_id` fallback (`train_controller.py:446-460`)
- `travel_direction: Optional[str]` — derived from cab + position facing direction (`train_controller.py:462-496`)
- `lookahead: List[LookaheadPoint]` — pure derived (`train_controller.py:498-507`); calls `_calculate_lookahead()`
- `target_speed: float` — `lookahead[0].speed_limit_kmh` (`train_controller.py:509-524`)
- `speed_limit_reason: Optional[str]` — exposes `_last_speed_limit_reason` (`train_controller.py:526-536`)

Latches: see Phase-3 mailbox section above. Each latch's writer/reader/gating must come from the design doc's Latch Table.

References (clickable in the inspector):
- `train_id` → Train
- `received_path_plan` → Path (live; may be None)
- `ato_destination_block_id` → Block
- `current_span.occupied_blocks[]` (when shape allows) → Block list
- `pending_path_cancel_request.expected_path_id` → Path

Key methods to surface (read-only previews, optional):
- `get_live_path_data()` (`train_controller.py:1250`) — returns active path's tile slice + head index
- `get_monitor_lookahead()` (`train_controller.py:4305`) — full UI-ready lookahead (already used by train monitor)
- `to_dict()` (`train_controller.py:4059`) — what the existing API returns

#### `TrainPhysics` (`train_physics.py:73-`)

- `train_id: str`, `network`, `controller` — references
- `_last_occupied_blocks: Set[str]` — internal delta tracker
- `_caution_blocks: Set[str]` — internal
- `last_block_transitions: Dict[str, Set[str]]` — maps `"entered"`/`"vacated"` → block-id sets
- `_tick_count: int`, `_sync_validation_interval: int`
- `_force_stop: bool` — transient flag

Useful derived info:
- Last `PhysicsSpanUpdate` emitted (could be cached as a debug-only field; not currently stored — design decision needed)

---

### 1.3 Per-resource

#### `Block` (`block.py:24-251`)

Stored:
- `block_id: str`, `name: str`
- `length_cm: float`, `min_radius_cm`, `max_speed_kmh`
- `state: BlockState` (FREE / RESERVED / OCCUPIED / CAUTION)
- `occupied_by: Optional[str]` — train id
- `current_voltage: float`
- `reserved_by_path: Optional[str]` — path id
- `reserved_direction: Optional[str]` (N/E/S/W)
- `disabled: bool`

Derived helpers (callable predicates):
- `is_free()`, `is_reserved()`, `is_available_for_path(path_id, train_id)`

References:
- `occupied_by` → Train
- `reserved_by_path` → Path

#### `Switch` (`switch.py:29-288`)

Stored:
- `switch_id`, `x`, `y`
- `main_length_cm`, `diverge_length_cm`, `main_radius_cm`, `diverge_radius_cm`
- `switch_type`, `name`, `notes`
- `locked_by_path: Optional[str]` — Switch-entity lock
- `locked_state: Optional[int]` (0=MAIN, 1=DIVERGE)

Derived (require network parameter):
- `get_active_length(state)`, `get_active_radius(state)`
- `is_locked()`, `is_configured(state)`
- `get_current_state(network)` — reads from underlying `TrackSegment.switch_state`

References:
- `locked_by_path` → Path
- `(x, y)` → TrackSegment

Discrepancy: there are **two** lock locations — `Switch.locked_by_path` and `TrackSegment.locked_by_path` (`track.py:198-200`). The inspector should expose both side-by-side so a stuck switch is debuggable. See `Switch.to_dict(network)` which already does this (`switch.py:227-261`).

#### `TrackSegment` (`track.py:160-`)

- `x: int`, `y: int`, `track_bits: int`
- `dead_end_ports: Set[str]`
- `_switch_state: int`
- `signal_ids: List[str]`, `station_id: Optional[str]`, `block_section_id: Optional[str]`, `switch_id: Optional[str]`
- `length: float`, `curve_radius: Optional[float]`
- `occupied_by: Optional[str]`, `reserved_by: Optional[str]`
- `wear_level: float`, `last_maintenance`, `maintenance_count: int`
- `locked_by_path: Optional[str]`, `locked_state: Optional[int]`, `path_direction: Optional[str]`

References:
- `block_section_id` → Block
- `switch_id` → Switch
- `signal_ids[]` → Signal list
- `station_id` → Platform/Station
- `occupied_by` / `reserved_by` / `locked_by_path` → Train / Path

#### `Path` (`path.py:259-`)

Stored:
- `path_id`, `status: PathStatus`, `priority: int`
- `start_block_id`, `end_block_id`, `start_station_id`, `end_station_id`
- `path_tiles: List[Tuple[int,int]]`
- `block_sequence: List[str]`
- `switch_positions: Dict[(x,y), int]`
- `train_id: Optional[str]`, `dispatcher_id: str`
- `current_block_index: int`, `blocks_cleared: Set[str]`
- `rear_hold_distance_cm: float`
- `flank_protection_switches: Dict[(x,y), int]`
- `overlap_blocks: List[str]`, `overlap_distance_cm: float`
- `approach_locked_blocks: Set[str]`
- `created_at`, `activated_at`, `completed_at`

Derived helpers (`path.py:319-`):
- `get_tile_directions() -> Dict[str, str]`
- `get_exit_direction() -> Optional[str]`
- `get_last_tile_entry_edge()`
- `is_active()`, `is_set()`, `is_completed()`, `is_cancelled()`, `can_be_extended()`, `can_be_cancelled()`, `has_train()`
- `get_current_block()`, `get_remaining_blocks()`, `get_cleared_block_count()`, `get_progress_percentage()`
- `get_all_locked_switches()`

References:
- `train_id` → Train
- `block_sequence[]` → Block list
- `start_block_id`, `end_block_id` → Block
- `start_station_id`, `end_station_id` → Station/Platform
- `switch_positions` keys → Switch (by position)
- `flank_protection_switches` keys → Switch (by position)

History: `Path` instances live in `PathDispatcher.active_paths` regardless of status, so "history" = same registry filtered by `status in {COMPLETED, CANCELLED}`. There is no separate retired-path archive.

#### `Platform` (`railway_platform.py:37-`) and `Station` (`station.py:14-`)

- Platforms are physical (positions, length, side). Stations are logical groupings of platforms.
- Both have `to_dict()` already; surface their stored fields directly. Lower priority for v1 because they are mostly static.

---

### 1.4 Schedule layer

#### `Route` (template) (`train_route.py:174-`)

Stored:
- `route_id`, `name`
- `waypoints: List[Waypoint]`
- `mode: RouteMode` (ONE_OFF / CIRCULAR / ROUND_TRIP)
- `assigned_train_ids: List[str]`
- `is_active: bool`, `started_at: Optional[float]`
- `last_error: Optional[str]`, `error_time: Optional[float]`
- `current_waypoint_index: int`, `direction: int` — runtime carry-overs

References:
- `assigned_train_ids[]` → Train list
- `waypoints[].platform_id` → Platform

#### `Waypoint` (`train_route.py:50-`)

- `platform_id`, `dwell_time_sec`, `flexible_platform`, `required_platform_side`, `travel_direction_policy`, `timing_mode`, `target_time`

#### `RouteExecutionState` (per-train RouteRun) (`route_executor.py:82-114`)

Stored:
- `train_id`, `route_id`
- `current_waypoint_index`, `direction`
- `route_start_time`, `arrived_at_platform_time`
- `waiting_for_path: bool`
- `last_waypoint_platform_id`, `resolved_platform_id`
- `path_cancelled: bool` — v1 latch consumed by Phase-1 cleanup
- `route_state: str` — RUNNING / NO_PATH / DEVIATED / FINISHED / DEACTIVATED
- `last_cancellation_kind: Optional[PathCancellationKind]`
- `active_path_id: Optional[str]`

References:
- `train_id` → Train
- `route_id` → Route
- `active_path_id` → Path
- `resolved_platform_id` → Platform

Note: the design doc names this concept `RouteRun`; the code keeps the legacy class name `RouteExecutionState`. Same thing.

---

### 1.5 Discrepancies (do not fix here)

These came up during the survey. They are bugs/gaps to file separately, not Inspector work:

1. `NotificationCenter` is named in the design but not present in code. Only `TrainController.pending_notifications` exists.
2. `Train.current_path` is a stale cache (per the docstring at `train.py:410-416`); the source of truth is `PathDispatcher` via `TrainController.get_path_tiles()`. The inspector should display both side-by-side and label the cache.
3. `Train.assigned_path_id`, `Train.travel_direction`, `Train.active_cab`, `Train.ato_running`, `Train.ato_destination_block_id` are still stored on `Train` but `TrainController` is the canonical owner. Worth showing both so drift is visible.
4. Switch lock status is stored in two places (`Switch.locked_by_path` and `TrackSegment.locked_by_path`). Display both.
5. `TrainController.current_span` is typed `Optional[Any]` (not the canonical `TrainSpan` schema yet, per `train_controller.py:380`). The inspector should show whatever shape arrives.
6. `TrainController.travel_direction` falls back to `position.facing_direction` — see `train_controller.py:474-477` — because controller-owned span isn't fully wired yet. Worth labeling that explicitly.

---

## 2. API design

All inspector endpoints live under `/api/inspector/`. Handlers go in a new file `routes/inspector.py` and are registered from `routes/__init__.py:register_all`. Backend services injected via `AppState` (`app_state.py`): `network`, `path_dispatcher`, `sim_controller`. The same lock rule applies — read paths can run lock-free for snapshot dumps; if any read crosses entity boundaries (e.g. building lookahead) it must take `state.simulation_lock`.

### 2.1 `GET /api/inspector/index`

Shallow listing of every inspectable entity. No nested data — just enough for the entity tree.

**Response:**

```json
{
  "trains": [{"id": "T1", "label": "Train T1", "state": "RUNNING_MOVING"}],
  "paths_active": [{"id": "PATH_A_TO_B_001", "status": "active", "train_id": "T1"}],
  "paths_history": [{"id": "PATH_X_TO_Y_002", "status": "completed", "train_id": "T1"}],
  "blocks": [{"id": "BLK_001", "name": "Central", "state": "free"}],
  "switches": [{"id": "SW_5_10", "x": 5, "y": 10, "locked_by_path": null}],
  "platforms": [{"id": "PLAT_01", "name": "Platform 1"}],
  "stations": [{"id": "STN_01", "name": "Central Station"}],
  "routes": [{"id": "ROUTE_01", "name": "Loop", "is_active": true, "assigned_train_ids": ["T1"]}],
  "route_runs": [{"train_id": "T1", "route_id": "ROUTE_01", "route_state": "RUNNING"}],
  "singletons": [
    {"key": "simulation_controller", "label": "SimulationController"},
    {"key": "path_dispatcher", "label": "PathDispatcher"},
    {"key": "route_executor", "label": "RouteExecutor"},
    {"key": "block_manager", "label": "BlockManager"}
  ],
  "tracks_count": 412,
  "controllers_count": 1,
  "physics_count": 1
}
```

Performance: this iterates every registry but reads only static identity fields — cheap at any scale this app sees (under a few hundred entities). Recommend cache TTL = 1 tick (~100 ms).

### 2.2 `GET /api/inspector/entity/<entity_type>/<entity_id>`

Full inspectable state of one entity. Response shape is uniform across types so the frontend can render it generically.

`entity_type` ∈ `train`, `controller`, `physics`, `path`, `block`, `switch`, `track`, `platform`, `station`, `route`, `route_run`, `singleton`.

For singletons, `entity_id` is the singleton key (`simulation_controller`, `path_dispatcher`, etc.). For tracks, `entity_id` is `"x,y"`. For route runs, `entity_id` is `train_id`. Everything else uses the natural id.

**Response:**

```json
{
  "type": "controller",
  "id": "T1",
  "display_name": "TrainController for T1",
  "items": [
    {"name": "power_on", "value": true, "type": "bool", "source": "stored"},
    {"name": "active_cab", "value": "A", "type": "str", "source": "stored"},
    {"name": "travel_direction", "value": "E", "type": "str", "source": "property",
     "computed_from": ["active_cab", "current_span", "position.facing_direction"],
     "doc_ref": "train_controller.py:462-496"},
    {"name": "received_path_plan", "value": "PATH_A_TO_B_001", "type": "ref",
     "source": "property",
     "computed_from": ["path_dispatcher.get_active_path"],
     "ref": {"entity_type": "path", "entity_id": "PATH_A_TO_B_001"}},
    {"name": "lookahead", "value": [...first 10 points...], "type": "list[LookaheadPoint]",
     "source": "property", "truncated": true, "full_count": 27},
    {"name": "pending_path_cancel_request", "value": null, "type": "Optional[PendingPathCancelRequest]",
     "source": "latch",
     "latch": {"writer": "TrainController.activate_emergency_cmd / phase-1 / phase-2",
               "reader": "SimulationController._consume_pending_path_cancel_request",
               "gating": "is_stopped() == True; survives across ticks otherwise"}},
    {"name": "current_speed_kmh", "value": 42.7, "type": "float", "source": "stored"}
  ],
  "references": [
    {"entity_type": "train", "entity_id": "T1", "label": "Train"},
    {"entity_type": "path", "entity_id": "PATH_A_TO_B_001", "label": "active path"}
  ]
}
```

Field source tags:
- `stored` — actual `dataclass` field
- `property` — derived `@property`; include `computed_from` (list of inputs)
- `latch` — pending Phase-3 mailbox field; include `latch.writer/reader/gating`
- `external` — read from another component via reference call (e.g. `path_dispatcher.get_active_path(train_id)`)

For collection-typed values (lists, dicts, sets), the response truncates to the first 50 entries and sets `truncated: true` plus `full_count`. The frontend asks for full data through a separate endpoint when the user expands the field.

### 2.3 `GET /api/inspector/entity/<entity_type>/<entity_id>/field/<field_name>`

Returns the full unbounded value of one field. Used when the user expands a truncated list. No nesting limits beyond what JSON serialization can handle.

### 2.4 `GET /api/inspector/snapshot`

Whole-world dump. Dictionary keyed by entity type, each value a dict keyed by entity id, each leaf the same shape as `/entity/<type>/<id>` but with collections truncated to 10 items by default (configurable via `?max_items=N`).

Useful for diff-based debugging (capture, run scenario, capture, compare). The Lite poller uses this too.

Performance:
- For 1 train, 50 blocks, 10 switches, 5 paths, 1 route the response is ~30 KB JSON. Cheap.
- The expensive piece is `lookahead` per train — it walks the path. `_calculate_lookahead` runs once per controller tick already, so reading it once more in the snapshot adds negligible cost.
- Recommend snapshot endpoint use `max_items=10` by default and live behind `state.simulation_lock` for consistency.

### 2.5 Polling & frequency

- Default poll: 1 Hz (1000 ms). Matches the user's stated comfort.
- The `index` endpoint can poll faster (250 ms) since it's static fields only.
- Snapshot is on-demand only; never auto-poll.
- Per-entity detail polls at 1 Hz when an entity is open.
- v1 uses HTTP polling; WebSocket push is out of scope.

### 2.6 Endpoint summary

| Endpoint | Method | Use |
|---|---|---|
| `/api/inspector/index` | GET | Sidebar tree |
| `/api/inspector/entity/<type>/<id>` | GET | Detail pane (1 Hz refresh) |
| `/api/inspector/entity/<type>/<id>/field/<name>` | GET | Expand truncated field |
| `/api/inspector/snapshot` | GET | Full dump for diff/export |

Total: 4 endpoints. All read-only. v1 is read-only across the board.

---

## 3. UI design

### 3.1 Global floating Inspector panel

**Toggle.** A new top-bar button labelled "Inspector" with an eye icon, placed next to the existing `Editor` / `Simulation` mode buttons in `static/index.html` (~line 43-46). The button toggles a draggable, resizable floating panel with a title bar.

**Layout (split):**

```
+-------------------------+----------------------------------------+
|  Entity tree (sidebar)  |   Detail pane                          |
|                         |                                        |
|  [search box]           |   <type> <id>                          |
|  > Trains (2)           |                                        |
|    > T1 [RUNNING_MOVING]|   ┌─────────────────────────────────┐ |
|    > T2 [OFF]           |   │ Item            Value    Source │ |
|  > Controllers (2)      |   │ power_on        true     stored │ |
|  > Paths active (1)     |   │ active_cab      "A"      stored │ |
|  > Paths history (4)    |   │ travel_direction "E"     prop   │ |
|  > Blocks (50)          |   │ received_path_plan PATH_… (link)│ |
|  > Switches (10)        |   │ lookahead       [27]     prop   │ |
|  > Routes (1)           |   │ pending_power_off false  latch  │ |
|  > Route runs (1)       |   └─────────────────────────────────┘ |
|  > Singletons (4)       |                                        |
|                         |   [auto-refresh: ☐ off  ☑ 1 Hz]        |
+-------------------------+----------------------------------------+
```

**Behaviors:**
- Search box filters entity tree by id (case-insensitive substring).
- Auto-refresh checkbox; default ON at 1 Hz.
- Clicking any reference (e.g. `received_path_plan: PATH_A_TO_B_001`) loads that entity in the same detail pane and pushes a back-history entry.
- Forward/back buttons navigate the history.
- Diff highlighting: when a refresh changes a value, briefly flash the row yellow (CSS transition, 800 ms).
- Rows are collapsible per source group (Stored / Properties / Latches / External) — header with chevron.

**Source badges:**
- `stored` — gray pill
- `property` — blue pill, tooltip = `computed_from` list + file:line `doc_ref`
- `latch` — orange pill, tooltip = `writer / reader / gating`
- `external` — green pill, tooltip = name of the component being read

**Optional v1: source-code jumps.** When `doc_ref` is present, the badge is a hyperlink that opens `/static/source-view.html?path=train_controller.py&line=462` in a new tab. If a static source-view page is too much work, the doc_ref string is shown verbatim in the tooltip so the user can grep.

**Persistent state.** Panel position, size, and last-selected entity persist in `localStorage` under `inspector.state`. Auto-refresh setting persists too.

**Implementation surfaces:**
- New file `static/js/inspector.js` — UI logic, polling, rendering.
- New file `static/css/components/inspector.css` — panel styling.
- Add panel HTML stub to `static/index.html` (one extra `<div id="inspector-panel" class="hidden">…</div>`).
- Add toggle button to `static/index.html` header controls.
- Wire button event in `static/js/app.js` next to the existing mode-switch handler.

### 3.2 Per-editor inspector sections

The user already opens secondary-sidebar editors for these entities:

| Entity | Editor entry point | File | Where to insert "Inspector" section |
|---|---|---|---|
| Train | `Editor.showTrainControlPanel(trainId)` (`editor.js:441`, delegates to `editor-trains.js`) | `editor-trains.js` | Append a collapsible `<details>` block at the bottom of the rendered control panel (`editor-trains.js:449`-area); section id `train-inspector-${trainId}` |
| Block | `Editor.showBlockEditor(blockId)` (`editor.js:393`) | `editor-blocks.js` | Append at bottom of editor; section id `block-inspector-${blockId}` |
| Switch | `Editor.showSwitchEditor(switchId)` (`editor.js:401`) | `editor-switches.js` | Append at bottom; section id `switch-inspector-${switchId}` |
| Path | `Editor.showPathEditor(pathId)` (`editor.js:2210`, delegates to `editor-paths.js`) | `editor-paths.js` | Append at bottom; section id `path-inspector-${pathId}` |
| Route | `routeHandler.showRouteEditor(routeId)` (`editor-routes.js:192`) | `editor-routes.js` | Append at bottom; section id `route-inspector-${routeId}` |

Each section is a collapsible `<details>` element rendering the same data as `/api/inspector/entity/<type>/<id>` but only for that entity.

Refresh policy: on by default at 1 Hz **only while the section is expanded**, to avoid 5 concurrent polls when nothing is open. When the user collapses, polling for that section stops.

Entities that **do not have an editor today**:
- TrainController, TrainPhysics — no dedicated editor exists. They are exposed only through the global inspector panel. (Some controller fields are surfaced in the train control panel; see existing `train-monitor.js`, but that is a monitor, not an editor.)
- Platform — no editor confirmed; entity surfaced only through the global panel.
- Station, Schedule — no per-entity editor; global panel only.
- Singletons — no per-entity editor; global panel only.

I confirmed this by grepping `static/js/editor*.js` for `show*Editor` definitions; the five rows above are the only ones that exist.

---

## 4. Implementation plan

Each phase is small enough to land in one PR.

### Phase 1 — Train inspector slice (smallest viable prove-out)

**Backend (`routes/inspector.py`):**
- `GET /api/inspector/entity/train/<train_id>` only.
- Response includes Train stored fields, the linked TrainController state via `to_dict()`, the linked TrainPhysics summary, and references.
- Wire `routes/__init__.py:register_all` to call `inspector.register(app, state)`.

**Frontend (`static/js/inspector.js`):**
- New file. Floating panel with a hardcoded "Trains" view. No tree yet.
- Buttons: select a train (dropdown of train ids), auto-refresh toggle, refresh-now button.
- Render the response as a flat table of `name | value | source`.
- Diff-flash on row updates.

**HTML/CSS:**
- Add toggle button to `static/index.html` and a panel container div.
- New file `static/css/components/inspector.css` with minimal styling.

**Success metric:**
- After cab switch (toggle from A to B), the user can read `controller.travel_direction` and `controller.active_cab` directly in the UI without diving into the code, and confirm that they match design_new.md's expected post-cab-switch values.

**Files touched:** `routes/inspector.py` (new), `routes/__init__.py`, `static/index.html`, `static/js/inspector.js` (new), `static/css/components/inspector.css` (new). Approx 400 LOC backend + 600 LOC frontend.

### Phase 2 — Path / Block / Switch inspector

- Add `/api/inspector/entity/path/<id>`, `/block/<id>`, `/switch/<id>`.
- Frontend gains entity-tree sidebar, search box, breadcrumb history.
- Reference clicks navigate within the panel.

**Success metric:** the user opens an active Path, sees `block_sequence`, clicks one block, sees its `reserved_by_path` matches.

### Phase 3 — Route / RouteRun / per-task table

- Add `/api/inspector/entity/route/<id>`, `/route_run/<train_id>`.
- Render the Route's `waypoints[]` as a small table (one row per waypoint) with `task_index`, `platform_id`, `dwell_time_sec`, `timing_mode`, and (for the active run) the per-task highlight indicating `current_waypoint_index`.

**Success metric:** the user sees the route run state machine values (`route_state`, `last_cancellation_kind`, `path_cancelled`) and can correlate them to a known bug.

### Phase 4 — Singletons and the index endpoint

- Add `/api/inspector/index` and `/api/inspector/entity/singleton/<key>` for the four singletons (SimulationController, PathDispatcher, RouteExecutor, BlockManager).
- Frontend now uses `index` to populate the entity tree dynamically.

**Success metric:** the user can see `PathDispatcher.path_sequence_counter`, `RouteExecutor.execution_states` count, and `BlockManager.history` recent entries from one place.

### Phase 5 — Per-editor inspector sections

- For each editor listed in §3.2, add a `<details>` block calling the same `/api/inspector/entity/<type>/<id>` endpoint.
- Polling activates on expand only.

**Success metric:** the user opens any editor and sees the same field-level data the global panel shows, without leaving the editor flow.

### Phase 6 — Polish: diff highlighting, source-code tooltips, snapshot endpoint

- Add `/api/inspector/snapshot` for the diff/export workflow.
- Frontend "Diff against last snapshot" button, comparing current data to the snapshot and highlighting changed rows.
- Property tooltips show `computed_from` + file:line.

**Success metric:** the user runs a scenario, takes two snapshots, and the inspector visualizes which fields differ.

---

## 5. Open questions for the user

These genuinely need decisions before implementation:

1. **Auto-refresh rate.** Is 1 Hz too slow when chasing fast bugs (e.g. cab switch happens within 200 ms)? Options:
   - 1 Hz (default, low cost)
   - 4 Hz (250 ms, slight cost on snapshot endpoint)
   - On-demand only (manual refresh button); no polling
2. **Snapshot recompute granularity.** Should `lookahead` be recomputed on every entity GET (cheap-ish but redundant if the controller already computed it this tick), or should the inspector just expose `controller._last_lookahead_cache` if/when one exists? v1 plan: recompute every read; revisit if profiling shows it.
3. **Persistent UI state.** Should the panel's open/closed state and selected entity survive a page reload? Default proposal: yes, via `localStorage`. Trade-off: stale state if the user reloads after a long absence.
4. **Read-only confirmation.** v1 is read-only inspection. Confirm there is no in-scope use case for editing fields from the inspector. (Editing live runtime fields is a footgun.)
5. **WebSocket push vs polling.** v1 uses HTTP polling. Confirm OK to defer WebSocket push to a later iteration.
6. **Discrepancies.** Several name/storage mismatches were found (NotificationCenter missing, dual storage of train cab/direction/path id, switch lock in two places). These are documented but not fixed by this work. Should they be filed as separate issues now, or addressed inline as the inspector surfaces them?
7. **Track inspector?** TrackSegment is a per-tile entity with a lot of denormalized references (block, switch, signal, station). Including it bloats the index endpoint. Proposal: do **not** index tracks; expose them only through the global panel via a "Track at (x,y)" lookup form. Confirm OK.

---

## Appendix A — Schema mapping cheat sheet (file:line)

For the engineer wiring the entity-handler functions:

| Entity | Source | `to_dict` (existing) |
|---|---|---|
| Train | `train.py:349` | `train.py:988` |
| TrainPosition | `train.py:21` | `train.py:209` |
| TrainController | `train_controller.py:276` | `train_controller.py:4059` |
| TrainPhysics | `train_physics.py:73` | `train_physics.py:1201` |
| Block | `block.py:24` | `block.py:56` |
| BlockManager | `block_manager.py:46` | (no to_dict — has `history` deque) |
| Switch | `switch.py:29` | `switch.py:227` |
| TrackSegment | `track.py:160` | (search for `to_dict` in track.py) |
| Path | `path.py:259` | `path.py:361` |
| PathDispatcher | `path_dispatcher.py:62` | (no to_dict — has `active_paths` registry) |
| Route | `train_route.py:174` | (search in train_route.py) |
| Waypoint | `train_route.py:50` | `train_route.py:135` |
| RouteExecutionState (RouteRun) | `route_executor.py:82` | (no to_dict; just a dataclass) |
| RouteExecutor | `route_executor.py:117` | (no to_dict; has `execution_states` dict) |
| Platform | `railway_platform.py:37` | (search in railway_platform.py) |
| Station | `station.py:14` | (search in station.py) |
| SpeedProfile | `speed_profile.py` | (search) |

---

## Appendix B — Latch table reference

The Phase-3 latches the inspector should annotate with writer / reader / gating come from the canonical Latch Table in `design_new.md` (Core Runtime Loop section). The inspector does not duplicate the rules — it references the table by name in tooltips. For each `pending_*` field the inspector displays:

- writer phase + entity
- reader phase + entity
- gating predicate (e.g. `is_stopped() == True`)
- multi-tick survival rule (e.g. "survives while train is rolling")

The Phase-3 latch consumer code in `simulation.py:189-316` is the runtime reference for these values; if the design and the code disagree, that is a discrepancy to file.
