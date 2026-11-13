# ATCS6 — Architecture Field Guide

A structural breakdown of the codebase, organized as a short book. Each section corresponds to one block in the two architecture diagrams: **Part I** walks the five layers of the whole system (top to bottom), and **Part II** zooms into the browser frontend. Every section opens with the question it answers.

This guide describes **what the code is and how it fits together**. For *behavior*
(the per-tick contracts, the ATO/ATC rules, the latch semantics), [design_new.md](design_new.md)
remains the source of truth; for the *user-facing* tour, see [README.md](README.md).

---

## How this book is organized

```
ATCS6
├─ Browser — frontend/ (TypeScript)        ......... Part II
│   ├─ App orchestrator (app.ts)
│   ├─ Editor (editor/)        + SimEditorPlugin
│   ├─ Renderer (renderer/)    + SimRendererOverlay
│   ├─ Side panels (panels/)
│   └─ Shared foundation (lib/)
│            ↓ REST · JSON          ↑ state snapshot (poll)
├─ Flask web layer                          ........ Part I · Ch. 2
│   └─ server + app  ·  routes/ (18)  ·  services/ (10)
├─ Simulation & control — the train computer ....... Part I · Ch. 3
│   └─ Tick loop  ·  TrainController (ATO/ATC/EB)  ·  Dispatcher
├─ Domain model — RailwayNetwork            ........ Part I · Ch. 4
│   └─ Blocks  ·  Switches & tracks  ·  Trains & paths · signals · stations
└─ Output & persistence                     ........ Part I · Ch. 5
    └─ Block power → HAL (TrackOutput)  ·  Persistence (JSON)
```

**Contents**

- **Part I — The system at a glance**
  - Ch. 1 — Browser (overview) · [Editor](#editor) · [Renderer](#renderer) · [lib + panels](#lib--panels)
  - Ch. 2 — Flask web layer · [server + app](#server--app--boot--wiring) · [routes/](#routes--rest-blueprints) · [services/](#services--domain-services)
  - Ch. 3 — Simulation & control · [Tick loop](#tick-loop--simulation--runtime) · [TrainController](#traincontroller--ato--atc--eb--manual) · [Dispatcher](#dispatcher--pathfind-reserve-route)
  - Ch. 4 — Domain model · [Blocks](#blocks--occupancy--power) · [Switches & tracks](#switches--tracks--the-rail-graph) · [Trains & paths](#trains--paths--signals-stations)
  - Ch. 5 — Output & persistence · [Block power → HAL](#block-power--hal--dc-volts-outputinterface) · [Persistence](#persistence--json-layouts-autosave)
- **Part II — The browser frontend in detail**
  - Ch. 6 — Bootstrap · [App orchestrator](#app-orchestrator)
  - Ch. 7 — The Editor · [facade](#editor-facade) · [core handlers](#core-layout-handlers) · [SimEditorPlugin](#simeditorplugin) · [tool-registry](#tool-registry)
  - Ch. 8 — The Renderer · [core](#renderer-core) · [draw handlers](#layout-draw-handlers) · [SimRendererOverlay](#simrendereroverlay) · [render-snapshot](#render-snapshot)
  - Ch. 9 — Side panels · [inspector](#inspector) · [train-monitor](#train-monitor)
  - Ch. 10 — Shared foundation · [api](#api) · [state-manager](#state-manager) · [geometry](#geometry-camera-pathfinders-graph-search) · [helpers](#helpers)
  - Ch. 11 — The seam · [Flask backend /api](#flask-backend--api)

---

# Part I — The system at a glance

The system is five layers. A browser frontend draws the layout and the running trains; a Flask web layer turns HTTP requests into domain operations through a thin service layer; a background **tick loop** drives the simulation, with a train computer (ATO/ATC) and a dispatcher deciding movement; a **domain model** (`RailwayNetwork` and its managers) holds every entity; and an output layer resolves DC block voltages behind a hardware seam while persistence reads and writes layouts as JSON. Requests flow down; a full state snapshot is polled back up for the UI.

## Chapter 1 — Browser (overview)

The browser layer is covered in full in **Part II**. The three sections here are the bird's-eye view of its blocks.

### Editor

> Explain the frontend editor/ plugin structure

[frontend/src/editor/](frontend/src/editor) is the layout editor. A single facade (`Editor`) owns the toolbar, the canvas mouse/keyboard interactions, the mode state machine, the properties panel, and a cluster of per-entity **handlers**. By default it edits the *layout only* (blocks, signals, platforms, stations, switches, tunnels); the simulation entities (trains, paths, routes, route-runs, speed profiles) are added by a registered **plugin** rather than baked in — so the same editor can run as a layout-only component. → see **Part II, Chapter 7**.

### Renderer

> Explain the frontend renderer/ modules

[frontend/src/renderer/](frontend/src/renderer) draws the layout on a dark isometric canvas. A `Renderer` orchestrator coordinates a set of per-entity draw **handlers** (tracks, blocks, signals, platforms, stations, tunnels, routes, debug) plus the shared screen-projection geometry. It draws *only* the layout; trains and live route state are painted by a registered **overlay** on top, so a renderer with no overlay is a pure layout viewer. → see **Part II, Chapter 8**.

### lib + panels

> Explain frontend/src/lib and panels

[frontend/src/lib/](frontend/src/lib) is the shared foundation every other module sits on: the `api` data-client (the single seam to the backend), the `state-manager` cache, the `camera`, the editor `pathfinders`/`graph-search`, and UI `helpers`/`toast`. [frontend/src/panels/](frontend/src/panels) holds the side panels — `inspector` (selected-entity details) and `train-monitor` (live train readout) — which subscribe to the state manager. → see **Part II, Chapters 9–10**.

## Chapter 2 — Flask web layer

### server + app — boot & wiring

> Explain server.py, app_factory, and bootstrap

These three modules form the boot layer that turns a bare Python process into a running railway simulator with a REST API. The real entry point is [`main()`](server.py:188) in [server.py](server.py), invoked under `if __name__ == "__main__"`. It parses CLI flags with `argparse` (`--no-demo`, `--skip-startup-tests`, `--skip-frontend-build`, `--no-sim`, `--no-autosave`, `--no-reload`, `--port` defaulting to 5001, and `--scene` whose choices come from [`available_scenes()`](bootstrap.py:75)). Before anything else it can activate a scene via [`set_current_scene()`](bootstrap.py:89), then runs a hard startup-test gate: unless `--skip-startup-tests` is passed (or this is a Werkzeug reloader child), [`run_startup_tests()`](server.py:49) runs [`validate_block_rules()`](scene_validation.py:21) plus the full `tests/test_backend.py` suite as a `pytest` subprocess, and calls `sys.exit(1)` if either fails — the server refuses to boot on a red suite. Next it builds the frontend bundle through `frontend_build.ensure_frontend_bundle(...)` so `static/dist/main.js` is current before serving.

With the gate passed, `main()` calls [`bootstrap_runtime()`](bootstrap.py:175) to construct the domain objects, registers `sim_controller.enter_safe_state` with `atexit` as a loss-of-control fail-safe, wraps everything in an [`AppState`](app_state.py:24), builds the Flask app with [`create_app(state)`](app_factory.py:31), starts the background threads via `runtime.start_all_threads(state, ...)`, and finally hands off to `app.run(...)` on the chosen port. (A separate module-level block guarded by `if __name__ != "__main__"` in [server.py:176](server.py:176) covers the WSGI/gunicorn import path, building state and starting threads once on import.)

[`bootstrap.py`](bootstrap.py) owns scene selection and runtime assembly. It keeps a registry [`_SCENE_MODULES`](bootstrap.py:55) mapping scene IDs (`demo7_2`, the default, and `demo7_1`, the rig with both switch motors working) to importable modules, resolved at import time from the `ATCS_SCENE` env var. [`bootstrap_runtime()`](bootstrap.py:175) either builds a populated network via [`create_demo_network()`](bootstrap.py:129) (which calls the scene's `create_comprehensive_demo()`) or an empty one via [`create_empty_network()`](bootstrap.py:143), then constructs the `PathDispatcher`, a `NotificationCenter`, the `SimulationController`, optionally the demo train(s) through [`setup_demo_train()`](bootstrap.py:152), and a `PersistenceManager` — returning all four as a tuple.

[`create_app()`](app_factory.py:31) in [`app_factory.py`](app_factory.py) is a thin Flask factory: it creates the app with a `static` folder, enables `CORS`, registers a custom `sint` URL converter ([`SignedIntConverter`](app_factory.py:25)) so negative tile coordinates route correctly, stashes the `AppState` on `app.extensions["state"]`, and installs an `@app.after_request` hook that logs any non-2xx response. It then calls [`register_all(app, state)`](routes/__init__.py:47) to wire up every route blueprint and `register_debug_routes(...)` for the debug inspector. [`AppState`](app_state.py:24) is the shared-singleton container the route handlers read from — a dataclass bundling `network`, `path_dispatcher`, `sim_controller`, `persistence_manager`, the `simulation_lock`, the `simulation_running`/`simulation_speed` flags, and a `speed_profile_registry` property that delegates to `network.speed_profiles` so there is one canonical store.

- **Boot order in `main()`**: parse args → activate scene → startup-test gate (`sys.exit(1)` on failure) → build frontend bundle → `bootstrap_runtime()` → register `atexit` safe-state → build `AppState` → `create_app(state)` → `start_all_threads()` → `app.run(port=...)`.
- **Startup-test gate**: [`run_startup_tests()`](server.py:49) — the scene-independent block-validation rules in [`scene_validation.py`](scene_validation.py) + a `pytest` subprocess on `tests/test_backend.py`; refuses to boot if either fails.
- **`create_app()` responsibilities**: static folder, CORS, `sint` converter, `AppState` on `app.extensions`, non-2xx response logger, then `register_all` + `register_debug_routes`.

### routes/ — REST blueprints

> List the routes/ REST blueprints and what each does

The `routes/` package holds 18 modules, each exposing a single `register(app, state)` function rather than a Flask `Blueprint` object — every endpoint is attached directly to the app with `@app.route(...)` inside that function. They are all registered explicitly (in fixed order) by [`register_all(app, state)`](routes/__init__.py:47) in [`routes/__init__.py`](routes/__init__.py), which is called from [`create_app()`](app_factory.py:64). All API endpoints share the `/api` prefix except `static_files`, which serves the SPA at `/`. The CRUD-heavy modules are thin HTTP layers over `services/` (parse JSON, call the service, translate typed exceptions to status codes); a few modules (`signals`, `stations`, `tunnels`, `paths`) still call `state.network` and `state.path_dispatcher` directly.

| Module | Resource / endpoints |
| --- | --- |
| [static_files.py](routes/static_files.py) | Serves `index.html` at `/` and any `static/<path>` asset (SPA fallback). No `/api` prefix. |
| [simulation.py](routes/simulation.py) | Server + sim lifecycle: `GET /api/network`, `GET /api/network/stream`, `POST /api/simulation/control` (start/stop/reset/set_speed), `release-failed-runtime`, `/api/health`, `POST /api/server/restart`, `/api/network/steps`, `/api/network/clear`, `/api/network/stats`, and `GET/PUT /api/scale`. |
| [layouts.py](routes/layouts.py) | Saved-layout persistence: `GET/POST /api/layouts`, `POST /api/layouts/<name>/load`, `DELETE /api/layouts/<name>`. Delegates to `layout_service`. |
| [tracks.py](routes/tracks.py) | Track-tile CRUD: `GET/PATCH/DELETE /api/track/<x>/<y>`, plus `POST /api/tracks/bulk` and `/api/tracks/bulk-delete`. Delegates to `track_service`. |
| [blocks.py](routes/blocks.py) | Block CRUD + membership: `GET/POST /api/blocks`, `GET/PATCH/DELETE /api/blocks/<id>`, `create-with-tracks`, `extend`, `tracks` add/remove, `voltage`, `end_tiles`, `can_extend`. Delegates to `block_service`. |
| [signals.py](routes/signals.py) | Signal CRUD + validation: `POST /api/signal`, `GET/PATCH/DELETE /api/signal/<id>`, `/api/signal/<x>/<y>`, `reachable-blocks`, `validate/<x>/<y>`. Calls `network` directly. |
| [stations.py](routes/stations.py) | Logical station-group CRUD: `POST/GET /api/station`, `GET/PATCH/DELETE /api/station/<id>`, plus `add-platform` / `remove-platform`. Calls `network` directly. |
| [platforms.py](routes/platforms.py) | Physical platform CRUD + tiles: `POST /api/platform`, `GET/PATCH/DELETE /api/platform/<id>`, `validate`, `extend`, `remove_tile`, `can_extend`, `assign_tile`/`unassign_tile`, `block_assignments`. Delegates to `platform_service`. |
| [trains.py](routes/trains.py) | Train CRUD + control: `GET/POST /api/trains`, `GET/PATCH/DELETE /api/trains/<id>`, plus power, set-cab, depart, stop, handle, mode, ATO go/stop, position, speed-profile, assign-route, lookahead, notifications, onboard. Returns the `train`/`controller`/`dispatcher_progress` sibling payload; delegates to `train_service`. |
| [paths.py](routes/paths.py) | Path acquisition/management: `POST/GET /api/paths`, `GET/DELETE /api/paths/<id>`, `extend`, `reachable-extensions`, plus `/api/dispatcher/paths` and `reachable*` queries. Calls `path_dispatcher` under the sim lock. |
| [train_routes.py](routes/train_routes.py) | Route *definitions* + lifecycle: `GET/POST /api/train-routes`, `GET/PUT/DELETE /api/train-routes/<id>`, `activate`/`deactivate`, `status`, `history`, `reachable-from`, `check-viability`. Delegates CRUD to `train_route_service`. |
| [route_runs.py](routes/route_runs.py) | Read-only live + historical `RouteRun` records: `GET /api/route-runs`, `GET /api/route-runs/<run_number>`, plus override write endpoints (`PATCH .../overrides`, `DELETE .../overrides/<field>`). |
| [speed_profiles.py](routes/speed_profiles.py) | Speed-profile CRUD: `GET/POST /api/speed-profiles`, `GET/PUT/DELETE /api/speed-profiles/<id>`. Delegates to `speed_profile_service`. |
| [switches.py](routes/switches.py) | Switch CRUD + state: `GET /api/switches`, `GET/PUT/PATCH/DELETE /api/switches/<id>`, `POST /api/tracks/<x>/<y>/switch`, `PUT /api/switches/<id>/state`. Delegates to `switch_service`. |
| [tunnels.py](routes/tunnels.py) | Tunnel-table CRUD: `GET/POST/DELETE /api/tunnels`; recomputes flank protection on change. Calls `network` directly. |
| [inspector.py](routes/inspector.py) | Debug snapshot: `GET /api/inspector/entity/train/<id>` — grouped Train/TrainController stored, derived, latch, and ATO-debug fields with `source`/`formula_doc` tags. |
| [settings.py](routes/settings.py) | Runtime-tunable config: `GET/PUT /api/settings` over `runtime_config.RuntimeConfig`. |

Two modules listed in [`routes/__init__.py`](routes/__init__.py) — `train_routes` and `route_runs` — split route *definitions* from per-run *execution records*. Note also that `inspector.register` is the package's inspector route; it is distinct from `debug_inspector.register_debug_routes`, which `create_app` registers separately.

### services/ — domain services

> Explain the services/ layer

The `services/` package is the layer between the thin route handlers and the domain entities (the `RailwayNetwork` and its managers, `SimulationController`, `PathDispatcher`, `PersistenceManager`). As the package docstring in [`services/__init__.py`](services/__init__.py:1) states, each service function takes an `AppState` plus structured kwargs and returns domain objects or raises a typed exception; it contains no HTTP concerns (no `request`, no `jsonify`, no status codes). This keeps the routes thin and lets non-HTTP callers — CLI tools, journey tests, scripted scenarios — invoke the same logic directly. Shared input validation lives in [`validation.py`](services/validation.py), whose [`coerce_nonneg(value, field)`](services/validation.py:17) enforces the common "a present, non-null geometry/config value must be a number ≥ 0" rule (the UI sends centimetres) and raises `ValidationError` on bad input. The error taxonomy lives in [`exceptions.py`](services/exceptions.py): a base [`ServiceError`](services/exceptions.py:17) with subclasses [`ValidationError`](services/exceptions.py:21) → 400, [`NotFoundError`](services/exceptions.py:25) → 404, [`ConflictError`](services/exceptions.py:29) → 409, [`GoneError`](services/exceptions.py:33) → 410, and [`ServiceUnavailableError`](services/exceptions.py:40) → 503; route handlers catch these and map them to HTTP status codes.

Several services deliberately hold `state.simulation_lock` while mutating entity fields so the single-threaded per-tick phase loop never observes a torn read — `block_service.update_block`, `layout_service` (all ops), `speed_profile_service` (all ops), and `train_service.reposition_train`/`assign_speed_profile`/route-run overrides. `switch_service` and `track_service` deliberately take no lock, matching the routes they replaced.

The entity services are:

- **[block_service.py](services/block_service.py)** — block CRUD, bulk construction (`create_block_with_tracks`, `extend_block`), track add/remove with edge-tile and continuity validation, the `can_extend_block` predicate, and a downward platform/station auto-delete cascade on `delete_block`. Raises `ConflictError` when disabling an occupied/reserved/CAUTION block.
- **[layout_service.py](services/layout_service.py)** — save/load/list/delete saved layouts over `PersistenceManager`, centralizing the pause-while-saving pattern and the snapshot→steps recovery flag.
- **[platform_service.py](services/platform_service.py)** — platform create/update/delete/extend, `remove_tile` (returns a tagged result dict), tile-to-block assign/unassign, and the `validate_placement`/`can_extend` predicates.
- **[speed_profile_service.py](services/speed_profile_service.py)** — `SpeedProfile` CRUD over the shared registry, with duplicate-id, default-profile (`DEFAULT_PROFILE_ID = "default"`), and in-use-by-trains guards.
- **[switch_service.py](services/switch_service.py)** — `Switch` CRUD, idempotent `create_switch_at_track`, the `disabled`/`manual` availability flags with admin-reject preconditions + `BlockManager` notify + flank recompute, and `request_position_change` (routes through `SwitchManager`, emits a `command_result` event).
- **[track_service.py](services/track_service.py)** — single-tile `update_track` (bits/dead-ends/switch-state/block membership) with orphan-impact pre-check, auto-switch creation, and tagged result dicts (`needs_confirmation`, `switch_locked`, `bits_rejected`, `removed`); plus `add_tracks_bulk`/`delete_tracks_bulk`, each recomputing flank protection.
- **[train_service.py](services/train_service.py)** — train CRUD (placement-aware create), reposition, speed-profile assignment, the route assign/retarget/unassign transaction (writes the Phase-3 route-abort latch, never mutates execution state inline), and the operator commands (power, cab, depart, handle, mode, ATO go/stop, release-emergency) that validate input and delegate to `TrainController`, returning its `CommandResult`.
- **[train_route_service.py](services/train_route_service.py)** — `Route` *definition* CRUD: id allocation, mode-string canonicalization, `last_edited_at` bookkeeping, leading-dwell rejection, structural-edit RouteRun teardown, activation gates (must end in a stop, priority 0–9), and per-field override push policies.

Most CRUD route modules call into exactly one service — `routes/blocks.py` → `block_service`, `routes/platforms.py` → `platform_service`, `routes/tracks.py` → `track_service`, `routes/trains.py` → `train_service`, `routes/train_routes.py` → `train_route_service`, `routes/speed_profiles.py` → `speed_profile_service`, `routes/switches.py` → `switch_service`, `routes/layouts.py` → `layout_service`. The remaining modules (`signals`, `stations`, `tunnels`, `paths`, `route_runs`, `simulation`, `inspector`, `settings`) bypass the service layer and operate on `state.network` / `state.path_dispatcher` / `state.sim_controller` directly.

## Chapter 3 — Simulation & control

### Tick loop — simulation & runtime

> Explain the simulation tick loop and runtime

The simulation advances in discrete ticks driven by a background thread and executed by [SimulationController](simulation.py). [`create_simulation_loop`](runtime.py) in [runtime.py](runtime.py) spins a daemon thread that wakes roughly every 100 ms (`time.sleep(0.1)`), measures the wall-clock delta since the previous wake, multiplies it by `state.simulation_speed`, and calls [`SimulationController.update(delta_seconds)`](simulation.py) under `state.simulation_lock`. Before integrating, the delta is run through [`_clamp_sim_delta`](runtime.py), which floors it at 0 and caps it at the module constant `MAX_SIM_DELTA_SECONDS = 1.0` ([runtime.py](runtime.py)). This clamp exists because a multi-second stall (lock contention, GC, a host that fell behind) would otherwise be integrated in one giant jump that overshoots a train's max speed and teleports its head past its authority, tripping a spurious ATC overspeed emergency brake; empirically that EB appears around a ~3.5 s step, so 1.0 s sits safely below it. Normal ticks — including a few-times speed multiplier — pass through unchanged as a single `update()` per tick, and when a hitch is clamped the surplus sim-time is simply dropped rather than replayed. A second daemon thread from [`create_autosave_loop`](runtime.py) wakes once a minute and writes a merged `_autosave` layout whenever `AUTOSAVE_INTERVAL` (300 s) has elapsed since the last save; [`start_all_threads`](runtime.py) launches both.

`update()` realizes the per-train three-phase transaction model from the design doc's "Core Runtime Loop." Each tick it first advances `simulation_time`, then — unless the `failed_runtime` halt gate is set — runs **Step 0**, [`_detect_block_occupancy`](simulation.py), which does one world-wide block-current read through the [`BlockOccupancyDetector`](block_occupancy.py), debounces and attributes confirmed block entries/exits to trains, and stashes each train's slice for its Phase-3 commit. It then refreshes route scheduling state via `RouteExecutor.update(...)` (time advancement only — it does not iterate trains) and asks [`RouteExecutor.order_runnable_trains(...)`](route_executor.py) for the ordered train ids. For each train it runs **Phase 1** (`RouteExecutor.tick_train_phase1`, the planning/reservation kernel, skipped while the controller is `OFF`) and then **Phase 2 + Phase 3** inside [`_update_train`](simulation.py). After every train has committed, blocks whose holder cleared this tick are dropped to 0 V, signals are refreshed from block state, and per-block occupied-time maintenance counters accrue. Throughout, a tick-local lookahead memo is enabled and `bump_lookahead_world_version()` is called at each phase boundary so a cached lookahead payload is never reused across an in-tick state change.

[`_update_train`](simulation.py) runs Phase 2 by calling `controller.tick(delta)` (which drains the operator command inbox, then computes ATC/ATO decisions) followed by `physics.tick(delta)`. Phase 3 then drains the controller's queued progress reports into `PathDispatcher.on_controller_progress`, performs the post-physics path-completion check via `is_stopped_at_authority_end()` / `try_complete_active_path(...)`, and consumes the four Phase-3 latches in canonical order (path-cancel → route-abort → power-off → route-deactivate). Movement itself lives in [TrainPhysics.tick](train_physics.py): it reads the controller-owned `TrainSpan`, asks the controller for a per-tick `PhysicsCommand` via [`produce_physics_command`](train_controller/__init__.py), integrates the next speed, converts speed to a distance (`kmh_to_cm_per_sec` applies model scale), walks the head/tail along the rail, and emits one `PhysicsSpanUpdate` per crossed resource boundary plus one final no-boundary callback back into `TrainController.on_physics_updated`. Physics is a pure executor — it never inspects controller mode fields — and it also writes track voltage through `compute_track_voltage`.

[speed_profile.py](speed_profile.py) supplies the per-train motor model that physics integrates against. A [`SpeedProfile`](speed_profile.py) carries the kinematic rates (`acceleration_rate`, `deceleration_rate`, `emergency_brake_rate`, `friction_rate`, quadratic drag), the per-notch `power_multipliers`/`brake_multipliers` tables, and a **two-stage calibration** — `power_speed_curve` (power% ↔ km/h) and `voltage_power_curve` (track voltage ↔ power%) — used by `speed_to_voltage_two_stage` when emitting voltage and by `power_percent_to_speed` for manual power-percent mode. `make_default_speed_profile` anchors a new train's curve to its own `max_speed`, and `resolve_memoized_default_profile` is the shared fallback both the controller and physics use so their reads cannot drift.

| Module | Role |
| --- | --- |
| [runtime.py](runtime.py) | Background `simulation_loop` + `autosave_loop` daemon threads; wall-clock delta measurement and `MAX_SIM_DELTA_SECONDS` clamp |
| [runtime_config.py](runtime_config.py) | Live-tunable [`RuntimeConfig`](runtime_config.py) singleton (ATC/ATO thresholds, safety margins, idle-hold timing). `apply_updates` coerces values and propagates them onto every consuming module global via the `_TARGETS` map, so changes from the Advanced settings panel take effect without rewiring |
| [runtime_payloads.py](runtime_payloads.py) | Single canonical home for cross-component dataclasses: `PhysicsSpanUpdate`/`TileTraversal`, `ControllerProgressReport`/`ResourceEvent`, the [`PhaseLatches`](runtime_payloads.py) aggregate with its `set_*`/`consume_*` helpers, `PathAcquisitionResult`, `FatalPhysicalEntryConflict`, the `Command` envelope, and the `PathDispatcher`↔`RouteExecutor` Protocols |

One halt path is worth noting: when a train would physically enter a resource held against it, `BlockManager` calls `enter_failed_runtime` with a typed `FatalPhysicalEntryConflict`; while that flag is set, `update()` only advances `simulation_time` and skips every train's Phases 1–3 until the conflict is manually cleared.

### TrainController — ATO · ATC · EB · manual

> Explain the train_controller package: ATO, ATC, EB, manual

[TrainController](train_controller/__init__.py) is the single authority for one train's operational state and control decisions — power, cab, mode, handle, ATC limits, and ATO driving. It is a dataclass whose `__post_init__` instantiates a set of focused sub-modules, each owning its own fields and holding a back-reference (`self._c`) to the controller; external callers reach state through the sub-module namespace (e.g. `controller.eb.active`, `controller.atc.speed_limit`). The central rule is that **ATO drives the train while ATC is the envelope it can never exceed**: ATO chooses a handle to track a speed curve, but that curve's ceiling is the most binding of the train's max speed, the ATC limit, and the current block limit, and if speed ever runs far enough past the ATC limit the controller escalates to an emergency brake. The controller's per-tick `tick(delta)` first drains the operator command inbox, then short-circuits on power-off or a latched EB, computes the live lookahead, refreshes ATC, escalates on overspeed, and finally runs the ATO or manual handle logic. Crucially, target speed and speed-limit reason are never stored — they are read live from position 0 of the canonical lookahead payload via the `target_speed` and `speed_limit_reason` properties.

The sub-modules divide the work cleanly:

- **[ato.py](train_controller/ato.py) — automatic driver.** `ATOComputer` owns the run lifecycle (`go()`/`stop()`, the `running` flag, the `ATOInfo` destination snapshot, and a per-tick `debug` payload). Its core is `compute_handle`: it computes `ceiling_at_zero = min(curve@0, train.max_speed, atc.speed_limit, atc.current_block_speed_limit)`, brakes (B8) if that ceiling is below the stopped threshold, otherwise samples the operational curve across a dt-scaled planning horizon, derives the required deceleration to stay under the curve, and maps `required_decel / op_decel` to a brake notch. When no braking is needed it powers up below the ceiling or coasts, and there is a dedicated "hold" branch that keeps a stopped train still when usable authority ends just ahead (avoiding the yard-throat creep-and-coast limit cycle). All handle changes pass through `rate_limit_handle` so the handle moves at most a notch or two per tick (EB bypasses the limiter).
- **[atc.py](train_controller/atc.py) — safety supervision.** `ATCComputer` owns the cached caps (`speed_limit`, `clearance_ahead_cm`, `current_block_speed_limit`) and recomputes them each tick. Every predicate has a module-level **pure function** plus a thin instance wrapper: `clearance_ahead_pure` sums block lengths along the train's own authority until an obstruction; `speed_limit_pure` returns the most binding of the kinematic safe-speed (from clearance and brake rate), the CAUTION cap, and the posted block limit, and — critically — returns `(0.0, "no_authority")` when there is no `received_path_plan` at all, making movement authority binary. Within `tick()` the controller actually sources the published limit from the lookahead curve's distance-0 anchor rather than the block-walk clearance, because the curve measures real tile geometry and does not under-count authority through zero-length blocks. Signals are display-only and do not feed the speed limit.
- **[eb.py](train_controller/eb.py) — emergency brake.** `EBWorkflow` owns the `active` latch. `activate()` accepts in any state (only gate: not already active), sets `active=True`, forces the handle to EB, clears `ato_running`, and — unless dwelling — arms a path-cancel latch. `release()` is strict: it accepts only when stopped, powered, the EB cancel latch has been consumed, and the pre-EB path authority has cleared, then lands the train in `READY` (or preserves dwell). The ATC overspeed path writes `eb.active = True` directly with its own non-idempotent semantics, the one sanctioned second writer.
- **[manual.py](train_controller/manual.py) — manual driving state.** `ManualDriving` holds two fields: `manual_mode_type` (`"simulation"` vs `"power_percent"`) and `manual_power_percent` (0–100). In `simulation` submode the operator's notched handle is integrated by physics; in `power_percent` submode the controller snaps to a calibrated target speed clamped against the ATC limit.

The remaining sub-modules wire the controller into the rest of the system:

- **[commands.py](train_controller/commands.py)** — `OperatorCommands` is the operator command surface. It owns `cab`, `control_mode`, and the `command_inbox`, and enqueued commands are drained at the top of `tick()` in a fixed `drain_order` (`power_on → set_mode → set_cab → set_manual_mode_type → set_handle/set_manual_power_percent → activate_emergency → release_emergency → power_off`, FIFO within each kind). `power_off` is stopped-only and defers the actual OFF transition, route teardown, and path cancellation to Phase 3 via the latch helpers. Each drained command emits a `command_result` notification.
- **[ops.py](train_controller/ops.py)** — `OperationalStateMachine` owns the canonical `state` (`OFF`/`READY`/`RUNNING_MOVING`/`RUNNING_AT_STATION`), `handle_position`, and the diagnostic `handle_reason`.
- **[physics_sync.py](train_controller/physics_sync.py)** — `PhysicsSync` owns the live `current_span` and `current_speed_kmh` (written by `on_physics_updated`), the Phase-3 `queued_progress_reports` mailbox, and the detector-sourced entered/vacated block slices. `on_physics_updated` also runs the reactive READY↔RUNNING_MOVING transition the same tick physics produced the speed change.
- **[route_hooks.py](train_controller/route_hooks.py)** — `RouteHooks` holds the five Action latches bundled in `PhaseLatches`; writers go through `set_path_cancel_request`/`set_route_abort`/`set_power_off`/`set_route_deactivate`/`set_emergency_request`. `SimulationController`'s Action steps consume the first four; the controller's command drain applies `emergency_request`.
- **[path_projection.py](train_controller/path_projection.py)** — `PathProjection` has no stored fields; `received_path_plan`, `assigned_path_id`, `ato_destination_block_id`, and `path_backed_run_active` all recompute live from `PathDispatcher.get_active_path(train_id)`, so authority auto-flips to `None` the moment the dispatcher moves the path off `ACTIVE`.
- **[notifications.py](train_controller/notifications.py)** — `Notifications` is the per-controller toast queue the frontend drains per train.
- **[train_controller_types.py](train_controller_types.py)** — shared dataclasses re-exported through the package: `LookaheadPoint`, `ATOInfo`, and the per-tick `PhysicsCommand` (a tagged union keyed on handle / target-speed / emergency-brake) that the controller hands to physics so physics never reads mode fields.

The envelope relationship shows up concretely in [`produce_physics_command`](train_controller/__init__.py): power-off yields a neutral coast, a latched EB yields an emergency-brake command, manual `power_percent` snaps to a target already clamped to the ATC limit, and ATO/manual-simulation hand physics the controller-set handle — but in every armed-motion case the live ATC limit (and the overspeed-EB escalation in `tick()`) caps whatever ATO or the operator asked for.

### Dispatcher — pathfind, reserve, route

> Explain the dispatcher: path_dispatcher, route_executor, pathfinder, resource_coordinator

The dispatcher is not one object but a small stack of cooperating modules that turn a high-level travel plan into the moment-by-moment movement authority a train is allowed to act on. At the bottom sits [railway_pathfinder.py](railway_pathfinder.py), whose `RailwayPathfinder.find_ordered_walk` runs a breadth-first search across the track graph and is the only place the four feasibility "levers" are wired in: inside `_walk_passes_filters` it rejects any candidate that crosses a disabled or manual resource, climbs a block steeper than the train's `train_max_slope_percent`, takes a curve tighter than `train_min_radius_cm` (checked per block via `Block.min_radius_cm` and per switch leg via `Switch.get_active_radius`), or violates a block's `allowed_travel_directions`; surviving walks are then ranked by estimated travel time (`_candidate_travel_time`, posted speed limits capped at the train's max) with distance as the tie-break, so all four of curve, slope, direction, and speed are honored rather than raw tile count alone. The unit of authority itself is the `Path` in [path.py](path.py): a born-ACTIVE record whose `resource_sequence` (an ordered list of `ResourceSequenceEntry` blocks and switches from tail to authority end) is the single source of truth, with `get_path_tiles` and `get_authority_end_block_id` derived live on every call. `PathDispatcher` in [path_dispatcher.py](path_dispatcher.py) owns that record's whole lifecycle — `create_path`, `extend_path`, and `cancel_path` — and is where conflict handling lives: it validates a candidate via `validate_path`, and under `BEST_EFFORT` mode it truncates a blocked walk down to the longest acquirable prefix (`_best_effort_truncate`), refusing to park a train on a two-way block straight off a switch or in a spot that would trap a reversing neighbor. Actual reservation is delegated to `ResourceCoordinator` in [resource_coordinator.py](resource_coordinator.py), a stateless facade over `BlockManager` and `SwitchManager` whose `reserve_segment` deliberately locks switches first and reserves blocks second, rolling back the switch locks if the block phase fails so the dispatcher sees one atomic call. One ACTIVE path per train is enforced as the first check in `create_path`, and committing to exactly the single shortest candidate (`candidates[:1]`) is what makes a train wait at a contested switch instead of detouring around the whole layout.

Above the path layer, a `Route` in [train_route.py](train_route.py) is the shared, reusable travel plan: an ordered list of `RouteTask` entries, each tagged `waypoint`, `stop`, or `dwell` and pointing at a `BlockTarget`, `PlatformTarget`, or `StationTarget` via the `task_location` tagged union, plus route-level policy like `mode`, `priority`, and `acquisition_mode`. Per-train mutable progress lives separately in `RouteExecutionState` (the doc's `RouteRun`), and `RouteExecutor` in [route_executor.py](route_executor.py) drives it: each tick `tick_train_phase1` calls `_find_next_task_target`, which forwards to `RouteTargetResolver` in [route_target_resolver.py](route_target_resolver.py) to turn the current task into a concrete `ResolvedRouteTarget` (a block id plus a resolved `forward`/`reverse` direction — the resolver always collapses `either` before any dispatcher call), and then the acquisition step asks the dispatcher for authority, calling `extend_path` when the train already holds a path and `create_path` otherwise. Route `priority` is purely per-tick acquisition order, not preemption: `order_runnable_trains` sorts running-phase runs ahead of starting-phase ones and then by descending priority, so a higher-priority train simply grabs contested blocks first that tick. The directed authority the train then actually follows is read through [lookahead_builder.py](lookahead_builder.py): `LookaheadBuilder.build` walks the live `received_path_plan.resource_sequence` from the head anchor outward into an ordered list of `LookaheadPoint`s (speed limits, signals, stations, and an `is_path_end` flag), which is the projection the controller's ATC consumes to decide how hard to brake.

- **Pathfinder** — [railway_pathfinder.py](railway_pathfinder.py): `find_ordered_walk` / `query_graph_walks` enumerate directed walks and filter them in `_walk_passes_filters` against disabled/manual flags, curve radius, slope, and `allowed_travel_directions`, then rank by `_candidate_travel_time`; results are cached per `(start, end)` and dropped on topology edits.
- **Path** — [path.py](path.py): `Path` is one train's movement authority; `resource_sequence` is the single source of truth, with `authority_end_block_id` and `path_tiles` derived live; status runs ACTIVE → COMPLETED/CANCELLED.
- **Dispatcher** — [path_dispatcher.py](path_dispatcher.py): `create_path` / `extend_path` / `cancel_path` build and tear down authority; `validate_path` plus `_best_effort_truncate` handle occupancy conflicts; one ACTIVE path per train; commits to a single shortest candidate so trains queue rather than detour.
- **Resource arbitration** — [resource_coordinator.py](resource_coordinator.py): `reserve_segment` locks switches then blocks (rolling back on failure) over `BlockManager` + `SwitchManager`; `rollback_segment` / `release_path_resources` unwind holds.
- **Route + executor** — [train_route.py](train_route.py) (`Route`, `RouteTask`, `RouteExecutionState`) and [route_executor.py](route_executor.py) (`tick_train_phase1`, `order_runnable_trains`): a route is a task sequence; the executor resolves the current task, acquires a path, and orders trains by run-phase-then-priority for per-tick acquisition.
- **Target resolver** — [route_target_resolver.py](route_target_resolver.py): `find_next_task_target` resolves a task to a concrete `ResolvedRouteTarget` (block + `forward`/`reverse`), the exact contract the dispatcher consumes.
- **Lookahead** — [lookahead_builder.py](lookahead_builder.py): `build` projects the live path's `resource_sequence` into the ordered `LookaheadPoint` list (limits, signals, `is_path_end`) the controller's ATC brakes against.
- **Data flow** — current `RouteTask` → `RouteTargetResolver` → `ResolvedRouteTarget` → `create_path`/`extend_path` → `RailwayPathfinder` chooses the walk + `ResourceCoordinator` reserves blocks/switches → `Path` authority → `LookaheadBuilder` feeds ATC, which follows it.

## Chapter 4 — Domain model

### Blocks — occupancy · power

> Explain the Block entity and the block managers

A `Block` is the railway's unit of occupancy and DC power — one electrically isolated track section whose rails all share voltage and current, and the smallest piece of track the control system reserves, occupies, and powers as a whole. It is defined in [block.py](block.py) as a dataclass that extends the shared [`TrackResource`](track_resource.py) identity base, carrying its physical configuration (`length_cm`, `min_radius_cm` for the tightest curve, `grade_percent` for gradient, and `speed_limit_kmh`) alongside a human-readable `name` and a `block_id`. Crucially, `Block.state` is not stored but computed on read from a handful of primitives in the strict priority order **OCCUPIED > CAUTION > RESERVED > FREE** (the `BlockState` enum lives in [enums.py](enums.py)): `occupied_by` holds the train physically present, `caution` is an optional `CautionState` record, and `reservation` is an optional `BlockReservation` record — so convenience reads like `reserved_by_path`, `reserved_direction`, and `caution_exit_edge` are all derived `@property` accessors. The block keeps its tiles in `ordered_tiles` (a list of `BlockTileEntry` `(x, y, entry_edge)` rows giving the canonical forward-traversal order) and an `allowed_travel_directions` set for one-way policy, plus an `occupied_seconds` maintenance counter. The block itself is a near-pure data container with only thin primitive mutators (`occupy`, `release`, `set_to_caution`, `clear`, `reserve_for_path`); the [`BlockManager`](block_manager.py) is the *sole* legitimate writer of physical and caution state.

`BlockManager` in [block_manager.py](block_manager.py) is the database-style lock manager that serializes every block transition so two callers never half-mutate a block: `occupy_blocks` is atomic all-or-nothing with rollback, `reserve_blocks_for_path` validates eligibility for every requested block before writing any field, and `on_block_tail_vacated` raises CAUTION behind a departing train then runs `_reevaluate_caution_blocks` to walk the live topology (through current switch positions) and clear caution once the train ahead is no longer moving away. Geometry and topology mutation live separately in [`SegmentGeometryManager`](segment_geometry_manager.py), which owns segment lifecycle — `create_segment`, `create_segment_with_tracks`, `extend_segment`, `delete_segment` (which refuses a segment in the middle of a multi-segment block, so a block is never left in two pieces), and the connectivity-driven `remove_tile_from_segment` that splits a segment into child segments when a tile removal disconnects it — and the single recomputation of traversal order. Segment creation and extension run their inputs through the pure rule functions in [segment_validation.py](segment_validation.py). Block membership (`segment_ids`) is owned by [`BlockGroupingManager`](block_grouping_manager.py). Both are owned by the aggregate root [`RailwayNetwork`](network.py), which holds the `blocks` dict and the derived `_tile_to_segment` reverse index; "which block owns this tile?" resolves tile -> segment -> block (a `Tile` does not store its own membership).

- **Entity vs. managers**: `Block` ([block.py](block.py)) is data + computed `state`; `BlockManager` ([block_manager.py](block_manager.py)) mutates runtime state (occupancy, reservation, caution); `SegmentGeometryManager` ([segment_geometry_manager.py](segment_geometry_manager.py)) mutates segment geometry (`length_cm`, `ordered_tiles`, tile membership); `BlockGroupingManager` ([block_grouping_manager.py](block_grouping_manager.py)) owns block membership (`segment_ids`).
- **Computed state**: `Block.state` derives from `occupied_by` / `caution` / `reservation` in priority order OCCUPIED > CAUTION > RESERVED > FREE; the `BlockState` enum is in [enums.py](enums.py).
- **Atomic acquisition**: `occupy_blocks` and `reserve_blocks_for_path` are all-or-nothing with rollback; conflicts are reported as typed `FatalPhysicalEntryConflict` payloads.
- **CAUTION cascade**: `on_block_tail_vacated` sets caution; `_reevaluate_caution_blocks` clears it by walking topology through live `Switch.position`.
- **Validation**: [segment_validation.py](segment_validation.py) enforces continuity, station boundaries, and that switches/diagonal crossings stay out of blocks.
- **Ownership**: blocks live in `RailwayNetwork.blocks` ([network.py](network.py)); the reverse index, not the track, records membership. *(Occupancy detection and DC power are covered in Chapter 5.)*

### Switches & tracks — the rail graph

> Explain switches and tracks, the rail graph

The rail graph is built from tiles, and every tile is a [`TrackSegment`](track.py) whose single source of truth is `track_bits` — a 6-bit integer where each bit encodes one physical rail shape on that tile (NS=1, EW=2, NE=4, NW=8, SE=16, SW=32, per the `BIT_TO_EDGES` and `EDGE_MASKS` tables in [track.py](track.py)). From those bits the segment derives everything else as cached `@property` reads: `edges` (which of N/E/S/W the rails touch), `num_paths`, `switch_points` (any edge with two or more exits), `is_switch`, and a geometric `track_type` ([`TrackType`](enums.py)). The graph is *traversal-aware*, not merely adjacency-based: `enumerate_traversals` yields every legal `(exit_edge, required_switch_state)` pair for a given entry edge as pure geometry, while `get_exits` / `get_active_exits` and `get_entry_for_exit` apply the live switch alignment to return only the currently-reachable exits — this is exactly what the pathfinder and the physics layer walk to move a train tile-to-tile. Curves are detected by *connection* rather than shape (`is_curve_connection`). A `TrackSegment` deliberately stores almost no runtime state — occupancy, reservation, block membership, and switch alignment are all derived `@property` shims that resolve through the owning `RailwayNetwork`.

A [`Switch`](switch.py) is the turnout entity, also a `TrackResource` subclass, and it — not the tile — is the authoritative home of switch alignment via `position` (the `SwitchState` enum, MAIN=0 / DIVERGE=1, in [enums.py](enums.py)). Each switch carries per-branch physical properties (`main_length_cm` / `diverge_length_cm`, `main_radius_cm` / `diverge_radius_cm`), a `flip_count` maintenance counter, and a rich v1 locking model: route-keyed physical occupancy in `route_owners` (keyed by a `frozenset` of the entry/exit edge pair, so two trains can share a scissors crossover on non-conflicting arcs per the `routes_conflict` rule), multi-owner future-authority `locks` keyed by `(path_id, source_switch_id)`, `flank_protection` neighbour relationships, and `train_protection_claims`. The data-only `apply_position_change` validates and writes `position`; `acquire_lock` / `release_lock` manage interlocking. Coordinating all of this is [`SwitchManager`](switch_manager.py), the mirror of `BlockManager`: it performs atomic all-or-nothing multi-switch lock acquisition (`acquire_locks_for_segment`, which also expands each primary claim into flank-protection requests on neighbours), path-scoped release, tail-vacate cleanup, clean physical entry with flank-to-protection conversion (`on_physical_entry`), and `request_position_change` — the single entry point that pairs `Switch.apply_position_change` with the cross-resource notification `BlockManager.on_switch_position_changed`. Tiles connect into one routable graph in [`RailwayNetwork`](network.py): `add_track` registers a segment and runs `propagate_from_tile` (a BFS wave that recomputes dead-ends), `get_neighbor` / `get_connection_edges` answer adjacency (including tunnel-linked dead-ends), and switches are registered via `add_switch`, which links the tile's `switch_id` and recomputes flank protection.

- **Tile geometry**: `track_bits` (6 bits) is the only stored connection data on [`TrackSegment`](track.py); `edges`, `switch_points`, `is_switch`, and `track_type` are derived.
- **Routable walk**: `enumerate_traversals` (pure geometry) and `get_active_exits` / `get_entry_for_exit` (switch-aware) define the directed graph the pathfinder traverses.
- **Switch authority**: [`Switch.position`](switch.py) (`SwitchState` MAIN/DIVERGE) is canonical, not the tile's `switch_state` (which resolves through to the owning switch).
- **Locking & occupancy**: `route_owners`, `locks`, `flank_protection`, and `train_protection_claims` model interlocking; `routes_conflict` decides physical compatibility.
- **Manager seam**: [`SwitchManager`](switch_manager.py) is the single serialization point for lock acquire/release and `request_position_change` (which notifies `BlockManager`); callers never call `Switch.apply_position_change` directly.
- **Graph assembly**: [`RailwayNetwork`](network.py) (`tracks`, `switches`) wires tiles via `propagate_from_tile`, `get_neighbor`, `get_connection_edges`, and `add_switch`; switches sit *between* blocks (they are block boundaries, never block members).

### Trains & paths — signals, stations

> Explain trains, paths, signals, and stations entities

A [`Train`](train.py) is the rolling stock as a *configuration* record only: it holds persistent fields like the `consist` (a list of `TrainCar`), `max_speed`, `min_radius_cm` (tightest curve it can take), `max_slope` (steepest grade it can climb), `priority`, `speed_profile_id` (a reference into `RailwayNetwork.speed_profiles`), and the `total_distance_cm` / `total_run_time_seconds` maintenance counters. Deliberately, the live physical position is *not* on `Train` — it lives on the controller as a [`TrainSpan`](train_span.py), the canonical four-field schema in [train_span.py](train_span.py). A `TrainSpan` records both physical ends as `SpanEndLocator`s (each a `tile` + `distance_cm` + `entry_edge`) plus the ordered `occupied_tiles` and `occupied_blocks` from `end_a` to `end_b`; the ends are stable, cab-independent geometric labels, so the head/tail and travel direction are derived from the controller's selected cab rather than stored on the span. `RailwayNetwork.place_train` builds the initial span by tracing the consist backward from its head tile through connected tiles, then occupies every block it covers.

Signals are multi-aspect block signals. A [`Signal`](railway_signal.py) ([railway_signal.py](railway_signal.py)) stores its `position`, its `detection_direction` (the way trains travel *toward* it), a `SignalType` (`BLOCK` in v1), its current `state` (the `SignalState` enum — GREEN, YELLOW, DOUBLE_YELLOW, RED — in [enums.py](enums.py)), and the `monitored_block_id` it watches. The aspect is *computed*, not set: [`SignalCalculator`](signal_calculator.py) ([signal_calculator.py](signal_calculator.py)), reached through `RailwayNetwork` forwarders like `update_signals_from_blocks`, derives each aspect in `compute_state` from the road ahead — the monitored block's state, whether the intervening switches route to it (`check_switch_routing`), the next block down the line (`find_next_block_after`), and the signal's `approaching_train_id`. Where a signal may be *placed* is governed by [signal_validation.py](signal_validation.py) (`validate_signal_placement`, `can_place_signal`), which forbids signals on switches, crossings, and complex junctions and pins each track type to its `VALID_DETECTION_DIRECTIONS`.

Stations come in two layers. A [`Platform`](railway_platform.py) ([railway_platform.py](railway_platform.py)) is the concrete, multi-tile stopping place: it owns `positions`, a `length_cm`, a `platform_side` (the `PlatformSide` enum), and `block_assignments` mapping each tile to its block, from which the derived `ordered_blocks` property names the two platform-end blocks for path planning. A [`Station`](station.py) ([station.py](station.py)) is purely logical — it has no tiles, only a `platform_ids` list grouping platforms under one name for flexible routing. Lifecycle for both lives in [`StationRegistry`](station_registry.py) ([station_registry.py](station_registry.py)) — `add_station`, `extend_station`, `delete_station`, and the station-group CRUD that enforces the "non-empty parent" rule. All of these entities are owned by the aggregate root [`RailwayNetwork`](network.py) in [network.py](network.py): `trains`, `signals`, `platforms` (aliased as `stations`), and `station_groups` are dicts on the network, alongside the `tile_to_platform_id` / `tile_to_station_id` reverse indices.

- **Train = config, span = live**: [`Train`](train.py) holds persistent properties; the live head/tail extent is a [`TrainSpan`](train_span.py) of two `SpanEndLocator`s plus ordered `occupied_tiles` / `occupied_blocks`, owned by the controller, seeded by `RailwayNetwork.place_train`.
- **Signals are computed**: [`Signal`](railway_signal.py) stores `detection_direction` / `monitored_block_id`; [`SignalCalculator.compute_state`](signal_calculator.py) derives GREEN/YELLOW/DOUBLE_YELLOW/RED from the block ahead, switch routing, the next block, and `approaching_train_id`. Placement rules are in [signal_validation.py](signal_validation.py).
- **Platform vs. Station**: [`Platform`](railway_platform.py) is physical tiles + `block_assignments` + `ordered_blocks` + `platform_side`; [`Station`](station.py) is a logical group of `platform_ids` with no tiles. CRUD for both is in [`StationRegistry`](station_registry.py).
- **Shared enums**: [enums.py](enums.py) defines `SignalState`, `BlockState`, `SwitchState`, `Direction`, `TrainState`, `PlatformSide`, `PathStatus`, and the lookahead `AtcLimitReasonCode`.
- **Ownership**: `trains`, `signals`, `platforms`/`stations`, and `station_groups` all live on [`RailwayNetwork`](network.py); the live "which route is this train on?" read comes from `RouteExecutor.assigned_route_id_for`, not from `Train`.

## Chapter 5 — Output & persistence

### Block power → HAL — DC volts, TrackOutput

> Explain block power and the hardware abstraction layer (TrackOutput)

ATCS6 models a classic DC analog layout, where each block is an electrically isolated stretch of track and a train is driven by the voltage on the rail beneath it rather than by a digital command on board. The job of deciding that voltage belongs to [`BlockPowerController`](block_power.py): every tick it walks the blocks a train occupies and writes one voltage onto all of them. It does not invent the number itself — `track_voltage` is owned by [`TrainPhysics.compute_track_voltage`](train_physics.py), and the controller's `_calculate_voltage_for_train` simply asks physics for the scalar and emits it, so all the manual-mode and emergency-brake policy stays in one place. The voltage shape follows the train's operational state: an `OFF` train gets `0V`; a stopped-but-lit train (`READY` or `RUNNING_AT_STATION`) gets the train's `lighting_voltage` (default `1.5V` on [`SpeedProfile`](speed_profile.py)), which keeps cab lights on without moving the train; and a moving train gets a voltage interpolated up from that same `lighting_voltage` floor toward `max_voltage` (`12.0V`) via `speed_to_voltage_two_stage`. The `lighting_voltage` doubles as the 0%-power anchor — at exactly that voltage the train holds, and just above it static friction breaks and it begins to roll — and direction is expressed as polarity, so the controller clamps output across a bipolar `-12V..+12V` range. Idle blocks are swept back to `0V` by `deenergize_unheld_blocks` (the per-tick path is event-driven through [`BlockManager`](block_manager.py)) so rail a train has left goes dead.

The hardware abstraction layer is the seam that lets this same logic drive either a simulation or a physical layout. The controller talks only to an [`TrackOutput`](block_power.py) addressed by logical `block_id`, never by pin or wire; today that is `SimulatedTrackDriver`, which just stores voltages in a dict, but a real Serial/I2C/GPIO backend would implement the same three methods (`set_voltage`, `get_actual_voltage`, `get_all_voltages`) and swap in unchanged. Switches have their own parallel seam in [switch_power.py](switch_power.py): [`SwitchPowerController`](switch_power.py) drives a point motor through a `SwitchOutput` where the target alignment (`0`=MAIN, `1`=DIVERGE) picks which coil is pulsed. The throw is blind — a solenoid point motor reports nothing back, so there is no continuity check and no confirmation step: `set_throw` reports only whether the command reached the device, never that the points seated. That controller also owns the switch's other electrical, its rail feed: a switch has no track channel of its own, so `switch_track_source` bonds it to the block at its main entrance and `get_switch_voltage` reads that block's voltage back. The read side of the boundary is [`OccupancyInput`](block_occupancy.py), the source for sensors no software output drives. Per [design_new.md](design_new.md) "Power, Block occupancy detection," that read is the occupancy authority: at the top of each tick (the Step-0 preamble) the runtime calls `read_block_currents()` once, and [`BlockOccupancyDetector`](block_occupancy.py) debounces it — quick to occupy, slow to clear — and attributes each confirmed change to a train before any train's phase chain runs. Finally, every output interface defines `enter_safe_state()`: when the runtime stops driving outputs (simulation stop or process shutdown) it zeros every block voltage and de-energizes every point motor.

- [`BlockPowerController`](block_power.py) emits one voltage to every block a train occupies; the value comes from `TrainPhysics.compute_track_voltage`, not from the controller.
- Voltage map: `OFF` → `0V`; `READY` / `RUNNING_AT_STATION` → `lighting_voltage` (`1.5V`, also the 0%-power / start-of-motion floor); `RUNNING_MOVING` → interpolated `lighting_voltage`→`max_voltage` (`12.0V`); polarity encodes direction.
- [`TrackOutput`](block_power.py) / `SimulatedTrackDriver` is the block seam; `SwitchOutput` ([switch_power.py](switch_power.py)) is the switch-throw seam (with a track-voltage continuity check on each throw); both are addressed by logical id only.
- [`OccupancyInput`](block_occupancy.py) is the sensor read side; [`BlockOccupancyDetector`](block_occupancy.py) debounce makes the confirmed current set the authority for `Block.occupied_by` via the global Step-0 read.
- [model_scale.py](model_scale.py) holds the prototype↔model scale factor (default `160.0`, N scale) and the `kmh_to_cm_per_sec` / `cm_per_sec_to_kmh` conversions physics uses to move a model-sized train at a scaled real-world speed.
- `enter_safe_state()` on each interface drives the fail-safe — every track voltage `0`, every point motor off — on simulation stop or shutdown.

### Persistence — JSON layouts, autosave

> Explain persistence and the layouts/ JSON format

Layouts are saved as flat JSON files on disk, managed entirely by [`PersistenceManager`](persistence.py) in [persistence.py](persistence.py); there is no database. Its `save_merged_layout` method writes one self-describing file per layout into the [layouts/](layouts) directory (atomically, via a `.json.tmp` rename) with a small metadata header — `version`, `name`, `description`, `modified` — wrapping two representations of the same layout. The first is `snapshot`, produced by [`RailwayNetwork.to_layout_dict`](network.py), which is the fast-loading primary format: it serializes the editor/configuration data only — `tracks`, `trains`, `blocks`, `signals`, `platforms`, `station_groups`, `switches`, and `tunnels`. The second is `steps`, the ordered list of construction operations that built the layout, generated by [`StepRecorder.generate_steps_list`](step_recorder.py). On load, `load_layout_with_fallback` tries the snapshot first by handing it to [`RailwayNetwork.from_dict`](network.py); only if that throws does it fall back to replaying the steps through [`StepPlayer.play_steps`](step_player.py), so a damaged or schema-drifted snapshot can still be rebuilt operation-by-operation. The `to_layout_dict` ↔ `from_dict` pair is the round-trip contract, and `from_dict` first calls `clear()` so a load can never inherit stale tile-to-block or tile-to-platform membership from the previously loaded layout.

A crucial scoping rule runs through all of this: persistence saves the *layout*, not the *running simulation*. Both `to_layout_dict` and the live frontend payload `to_dict` serialize the same entities, but only `to_dict` enriches them with live runtime state (block occupancy, signal aspects, switch locks, train positions) for rendering — `to_layout_dict` deliberately omits all of it, per the [design_new.md](design_new.md) "Out of Scope / Persistence" rule that runtime state is in-memory only. So a train's position mid-journey, its active path, and held block locks are never written to disk; reloading a layout gives you the track and the train roster, not a paused simulation. Autosave is wired as a background thread in [runtime.py](runtime.py): `start_autosave_thread` launches the `create_autosave_loop` loop, which wakes once a minute and, when `AUTOSAVE_INTERVAL` (300 seconds, i.e. ~5 minutes) has elapsed since the last save, writes the current network to `layouts/_autosave.json` under the simulation lock. Manual saves also bump `last_autosave_time`, so an explicit save pushes the next autosave out by a full interval.

- [`PersistenceManager`](persistence.py) writes one JSON file per layout into [layouts/](layouts); load is `load_layout_with_fallback`. State lives only in memory + these JSON files — no database.
- File format: a metadata header (`version`, `name`, `description`, `modified`) plus `snapshot` (fast-load primary, from [`to_layout_dict`](network.py)) and `steps` (construction-operation list, from [step_recorder.py](step_recorder.py)) as the recovery fallback replayed by [step_player.py](step_player.py).
- Snapshot keys: `tracks`, `trains`, `blocks`, `signals`, `platforms`, `station_groups`, `switches`, `tunnels` — configuration only; live runtime fields are excluded.
- Autosave: a daemon thread in [runtime.py](runtime.py) checks every 60s and saves to `layouts/_autosave.json` every `AUTOSAVE_INTERVAL` = 300s (~5 min); manual saves defer the next autosave.
- A running simulation is not saved: train positions, active paths, and locks are reconstructed fresh on load, never persisted.

---

# Part II — The browser frontend in detail

The frontend is a thin `App` orchestrator on top of two decoupled engines — the
`Editor` and the `Renderer` — that each have a layout-only core plus an *optional*
simulation extension (a plugin for the editor, an overlay for the renderer). A
shared `lib/` foundation sits underneath, and a single `api` client is the only
seam to the backend. Everything is bundled by Vite into `static/dist/main.js`;
[main.ts](frontend/src/main.ts) is the module manifest that imports every module
(with [app.ts](frontend/src/app.ts) last).

## Chapter 6 — Bootstrap

### App orchestrator

> Explain app.ts, the App orchestrator: how it boots, wires renderer and editor, polls the simulation, and toggles edit/sim mode

[main.ts](frontend/src/main.ts) is the bundle manifest — it imports `lib/`, the renderer cluster, the panels, the editor, and finally [app.ts](frontend/src/app.ts), whose bottom registers a `DOMContentLoaded` handler that constructs `new App()` and parks it on `window.app` for legacy inline handlers. The `App` constructor ([app.ts:44](frontend/src/app.ts:44)) is the wiring point: it builds `new Renderer('rail-canvas')`, registers a `SimRendererOverlay(stateManager)` on it, builds `new Editor(renderer, api)`, registers a `SimEditorPlugin`, then injects itself as `editor.host` and `editor.stateManager` so the editor calls back through injected references instead of globals. It also builds the `TrainMonitor`, wires the header controls / debug / settings / view / panel toggles, and kicks off the first `refreshNetwork()`.

At runtime the `App` owns the cross-cutting concerns that don't belong to either engine: the **simulation poll loop** (`startSimulationPolling` sets an interval that calls `refreshNetwork()` each tick while running — [app.ts:1511](frontend/src/app.ts:1511)), the play/pause and clear-fatal buttons, layout save/load, train selection with an opt-in follow-the-train toggle, lookahead-fetch throttling, and the top-level `editorMode: 'edit' | 'sim'` flag. `refreshNetwork()` ([app.ts:648](frontend/src/app.ts:648)) is the heartbeat: it fetches the network payload, pushes sim state into the `stateManager`, and requests a redraw — with a fast path that skips the full world redraw while a build tool is active.

## Chapter 7 — The Editor

### Editor facade

> Show how the Editor facade handles the toolbar, canvas mouse/keyboard input, the edit/sim mode state machine, and the properties panel

The facade ([editor.ts](frontend/src/editor/editor.ts), ~3,500 lines) owns four subsystems.

**Toolbar (registry-driven).** `coreTools()` returns `ToolContribution[]` grouped into Selection / Network / Platform / Block, each tagged `availableIn: ['edit']` or `['edit','sim']`. `_collectToolContributions()` merges core tools with plugin-contributed ones; `renderToolbar()` ([editor.ts:822](frontend/src/editor/editor.ts:822)) hands them to `renderToolGroups()` which builds a main-button + flyout-popover rail. Clicks are delegated through `_onToolbarClick` → `_dispatchTool` ([editor.ts:862](frontend/src/editor/editor.ts:862)), which branches on the contribution `kind`: `action` runs a panel/dialog and leaves the active tool unchanged; `tool` becomes the selected tool; `mode` enters a multi-step `EditorMode`.

**Canvas mouse/keyboard input.** A single wiring block ([editor.ts:1348–1895](frontend/src/editor/editor.ts:1348)) attaches `mousedown / mousemove / mouseleave / wheel / click / contextmenu` to the canvas plus document-level `mouseup` and several `keydown/keyup` handlers. Dispatch is by `this.selectedTool`: `mousedown` routes inspect/path tools, box/lasso/paint selection drags, and signal/tunnel placement to different start paths; `wheel` drives the camera zoom; `click` lets an active mode handle it first, otherwise selects the tile; keyboard provides tool shortcuts and Escape-to-exit.

**Two things both called "mode."** `editorMode: 'edit' | 'sim'` ([editor.ts:135](frontend/src/editor/editor.ts:135)) is the top-level edit-vs-operate toggle — passed to `renderToolGroups`, it *hides* tools whose `availableIn` excludes the current value (structural tools are edit-only). Separately, `activeMode` / `EditorMode` is the ModeManager ([editor.ts:1897–1945](frontend/src/editor/editor.ts:1897)) for multi-step interactions (route creation, place-train): `enterMode` exits any current mode, sets the tool to `mode.toolName`, then runs `onEnter`; `exitMode` clears `activeMode` first (so `onExit` can re-enter another) and runs `onExit` without changing the selected tool; while active it gets first shot at `onClick`/`onHover`.

**Properties panel.** Inspect-clicking a tile calls `selectElement(gridPos)` ([editor.ts:2885](frontend/src/editor/editor.ts:2885)): it fetches the tile via `api.getTrack(x,y)`, stores `selectedElement`, then `updatePropertiesPanel(track)` ([editor.ts:2937](frontend/src/editor/editor.ts:2937)) fills `.properties-panel` — decoding `track_bits` into segment names (NS/EW/NE/NW/SE/SW), edges, etc. — and refreshes the secondary tile editor unless that panel is pinned. Empty tiles get an `isVirtual` stub so you can still start drawing on them.

### Core layout handlers

> List the editor core layout handlers (block, signal, platform, station, switch, tunnel, ui, multi-select) and what each edits

These are instantiated directly by the `Editor` (layout-only; the 5 sim handlers come from the plugin). Six edit a real entity type; `ui` and `multi-select` are different in kind.

| Handler | File | What it edits |
|---|---|---|
| `SegmentEditorHandler` | [editor-segments.ts](frontend/src/editor/editor-segments.ts) | **Segments.** Create / extend / delete; edit name, length, speed limit, min radius, grade %; tile membership. Block-owned availability (Normal/disabled/manual) is routed to the owning block. Subscribes to live updates. |
| `SignalEditorHandler` | [editor-signals.ts](frontend/src/editor/editor-signals.ts) | **Signals.** Per-tile placement editor: type, detection direction, name; shows current aspect; invalidates signals along a path. |
| `PlatformEditorHandler` | [editor-stations.ts:33](frontend/src/editor/editor-stations.ts) | **Platforms.** Create/edit (name, side, length), extend/remove tiles, assign a platform tile to a block. |
| `StationGroupEditorHandler` | [editor-stations.ts:346](frontend/src/editor/editor-stations.ts) | **Logical station groups.** List/create/edit groups, add/remove member platforms, station-detail panel. |
| `SwitchEditorHandler` | [editor-switches.ts](frontend/src/editor/editor-switches.ts) | **Switches/turnouts.** Edit config; show operational throw state; a switches-list side panel with a live poller. |
| `TunnelEditorHandler` | [editor-tunnels.ts](frontend/src/editor/editor-tunnels.ts) | **Tunnels.** Inspect/edit a paired dead-end "tunnel" (the purple "T" mouth), delete the pair. |
| `EditorUIHandler` | [editor-ui.ts](frontend/src/editor/editor-ui.ts) | *Not an entity.* The sidebar **chrome**: secondary sidebar, working-set tabs, collapsible sections, JSON viewer, and the per-tile **bit / visual track-geometry editor**. |
| `MultiSelectActionsHandler` | [editor-multi-select.ts](frontend/src/editor/editor-multi-select.ts) | *Not an entity.* **Bulk actions** over a multi-tile selection: create a block from the selection, add/remove tiles to/from a block, delete tiles or affected blocks, apply block properties in bulk. |

### SimEditorPlugin

> Explain the SimEditorPlugin and the EditorPlugin extension point — how the sim handlers are bolted onto the layout-only core

The `EditorPlugin` interface ([editor.ts:56](frontend/src/editor/editor.ts:56)) is two methods: `register(editor)` to attach behavior, and an optional `tools(editor)` returning `ToolContribution[]` that get merged into the toolbar. [SimEditorPlugin](frontend/src/editor/sim-plugin.ts) is the one implementation: its `register` attaches the five simulation handlers onto the editor — `trainHandler`, `pathHandler`, `routeHandler`, `routeRunHandler`, `speedProfileHandler` — and its `tools()` contributes the Path / Train / Route tools, all `availableIn: ['edit', 'sim']` because dispatching stays available during operation (only the physical layout is frozen). The `App` opts in with `editor.registerPlugin(new SimEditorPlugin())`; a layout-only host simply never calls it, so the core editor stays layout-only and the sim handlers' code is not even pulled into that build (the core imports them as types only). This is the editor half of the editor/renderer separation — the same seam the reusable [layout-editor/index.ts](frontend/src/layout-editor/index.ts) barrel exports.

### tool-registry

> Explain the editor tool-registry and resource-picker, and how the toolbar is built from tool contributions

[tool-registry.ts](frontend/src/editor/tool-registry.ts) is the data model + renderer for the tool palette. Tools are declared as data, not markup: a `ToolContribution` names the tool (`id`, `label`, `icon`, `title`), the `group` it shows under, the `availableIn` modes, and a `kind` that decides how a click dispatches — `{type:'tool'}` (a stateful build/pick tool the canvas handlers read via `editor.selectedTool`), `{type:'mode', factory}` (enters an `EditorMode`), or `{type:'action', run}` (runs a panel/dialog and leaves the tool unchanged). `renderToolGroups(contributions, mount, {activeToolId, mode})` renders the vertical rail: an ungrouped tool is a direct button; each group collapses to one main button (with a `GROUP_ICONS` glyph) that opens a flyout popover of its tools; tools whose `availableIn` excludes the current `mode` are dropped, and a group left empty is omitted — so operation mode shows a clean palette. [resource-picker.ts](frontend/src/editor/resource-picker.ts) (`showResourcePicker` / `dismissResourcePicker`) is the shared popover for picking a target entity during a multi-step tool.

## Chapter 8 — The Renderer

### Renderer core

> Explain the Renderer core: the two stacked canvases, the rAF-coalesced redraw loop, the ResizeObserver, and the Camera

[renderer.ts](frontend/src/renderer/renderer.ts) is the orchestrator. As a module it imports the nine draw handlers plus the `Camera` directly, and it owns the two stacked canvases, an rAF-coalesced redraw loop, a `ResizeObserver`, mouse tracking, and the shared label-placement geometry the handlers borrow. `requestRedraw()` schedules a single `_flushRedraw` on the next animation frame, which builds the per-frame sim snapshot and then draws the layout layers in z-order, invoking any registered **overlays** at named phases (`afterStations`, `afterBlockDirection`, `top`) so trains and routes paint on top without the core knowing about them. The canvas background stays a fixed dark (`COLORS.BACKGROUND = '#0a0a0c'`) regardless of UI theme, and a `compassFromDelta` helper (matched byte-for-byte to the backend's `_compass_from_delta`) gives every handler the same grid-step heading. With no overlay registered, the `Renderer` is a pure layout viewer — the renderer half of the editor/renderer separation.

### Layout draw handlers

> List the renderer layout draw handlers (tracks, blocks, signals, platforms, stations, tunnels, routes, debug) and what each draws

The `Renderer` draws *only* the layout through these handlers (trains and live route-run state are drawn by the sim overlay). `RendererTrackHandler` is special — it owns the shared screen-geometry helpers the others borrow.

| Handler | File | What it draws |
|---|---|---|
| `RendererTrackHandler` | [renderer-tracks.ts](frontend/src/renderer/renderer-tracks.ts) | Track tiles (segments, switches with state badge + lock rings, dead-ends, crossings). Owns `getEdgePoints()` / `drawGridSegment()`. Also the editor preview overlays (build, invalid, hover, eraser, tile-extent). |
| `RendererBlockHandler` | [renderer-blocks.ts](frontend/src/renderer/renderer-blocks.ts) | Per-block tile coloring (by id/state), block-boundary markers, block detail labels (hover expansion), metrics, and the block preview path. |
| `RendererSignalHandler` | [renderer-signals.ts](frontend/src/renderer/renderer-signals.ts) | Signal heads (stick + lamp) and the dashed line linking each signal to the block it monitors. |
| `RendererPlatformHandler` | [renderer-platforms.ts](frontend/src/renderer/renderer-platforms.ts) | Platform bodies as oriented parallelograms plus a collapsed/hover label placed to avoid tracks and other platforms. |
| `RendererStationHandler` | [renderer-stations.ts](frontend/src/renderer/renderer-stations.ts) | One label per station group at the centroid of its platforms; hover expands to the full name and highlights member platform bodies. |
| `RendererTunnelHandler` | [renderer-tunnels.ts](frontend/src/renderer/renderer-tunnels.ts) | Tunnel mouths and (debug) pairing links; registers hit-test placements. |
| `RendererRouteHandler` | [renderer-routes.ts](frontend/src/renderer/renderer-routes.ts) | Route overlays: reachable-tile highlights, per-route path fills/borders, direction arrows, switch locks, and the conflict-flash animation. |
| `RendererDebugHandler` | [renderer-debug.ts](frontend/src/renderer/renderer-debug.ts) | Debug overlays: track-bit dots/visualization, coordinate + edge labels, switch-state badges, dead-end markers. |

### SimRendererOverlay

> Explain the RendererOverlay extension point and SimRendererOverlay — how trains, routes, and the placement ghost draw on top of the layout

[renderer-overlay.ts](frontend/src/renderer/renderer-overlay.ts) defines the extension point: a `RendererOverlay` is an object with a single `draw(phase, network, renderer)` method, and `OverlayPhase` is the union `'afterStations' | 'afterBlockDirection' | 'top'`. The `Renderer` calls every registered overlay at each phase as it paints, so an overlay only decides *when in the z-order* its content draws. [SimRendererOverlay](frontend/src/renderer/sim-overlay.ts) is the simulation layer: at `afterStations` it calls `renderer.routeHandler.drawRouteOverlays()` (reachable highlights, active routes, conflict flash); at `afterBlockDirection` it builds the per-frame sim snapshot with `buildRenderSnapshot(network, stateManager)` and draws each train via `renderer.trainHandler.drawTrain(...)`; at `top` it draws the train-placement ghost above the editor previews. Because the snapshot is the renderer's only dependency on the `stateManager` singleton, building it here (with `stateManager` injected) is exactly what lets the core renderer drop its `stateManager` import entirely.

### render-snapshot

> Explain render-snapshot and how a network payload becomes the renderer draw model

[render-snapshot.ts](frontend/src/renderer/render-snapshot.ts) is `buildRenderSnapshot` — built once per rAF flush, it produces an immutable per-frame view of sim state that handlers consume instead of reaching into the `StateManager` or mutable network fields directly. Its main job today is pre-resolving each train's controller **span** into a single camelCase shape (`occupiedTiles`, `leadingEnd`, `end_a`/`end_b`), reading the controller and dispatcher-progress slices from an injected `SnapshotStateManager`. The decoupling has two payoffs: renderer handlers become unit-testable with hand-built fixtures, and "what does the renderer see this frame?" becomes a single inspectable object. Track / signal / block state already flow through `network.*` so they aren't snapshotted yet — a documented place for future expansion.

## Chapter 9 — Side panels

### inspector

> Explain the inspector panel and how it self-bootstraps onto window

[panels/inspector.ts](frontend/src/panels/inspector.ts) renders the deep debug snapshot for a selected entity. It is imported by [main.ts](frontend/src/main.ts) purely for its side effect — it self-bootstraps the inspector panel onto `window` rather than being constructed by the `App` — so the editor handlers and other inline callers can reach it. Its payload comes from the backend's [inspector.py](routes/inspector.py) route (`/api/inspector/entity/train/<id>`), which returns the Train and TrainController fields grouped into stored / derived / latch / ATO-debug buckets, each value tagged with its `source` and a `formula_doc` pointer — so the panel is a structured, self-documenting view of one entity's internal state rather than a flat dump.

### train-monitor

> Explain the train-monitor panel and how it subscribes to the state manager

[panels/train-monitor.ts](frontend/src/panels/train-monitor.ts) is the live driving readout. The `TrainMonitor` class imports `api`, `stateManager`, `showToast`, and `getBlockStateColor`, and subscribes to the relevant `stateManager` entity types so it re-renders only when the selected train's `controllers` / `dispatcherProgress` slice actually changes — the `App` holds the unsubscribe handle as `trainMonitorUnsub`. It shows speed, handle position, the ATC limit and its reason, dwell/ATO status, and the lookahead **curve** (a series of `CurvePoint`s — `distance_cm`, `atc_limit`, `operational_limit`) that visualizes how far ahead the train can see and how hard it must brake. Because it reads the shared cache rather than polling itself, it stays in lockstep with the renderer's view of the same tick.

## Chapter 10 — Shared foundation (lib/)

### api

> Explain lib/api (the API class) and the LayoutDataClient / SimClient data contract it implements

[lib/api.ts](frontend/src/lib/api.ts) is the `API` class — the single Flask-backed client, with `baseUrl = '/api'` and one thin `fetch` wrapper per endpoint. It is typed to *implement* the data contract declared in [layout-data-client.ts](frontend/src/lib/layout-data-client.ts), which is deliberately split in two: `LayoutDataClient` (everything needed to view and edit the layout — track geometry, blocks, platforms, stations, switches, signals, tunnels, speed profiles, persistence, scale, and the network fetch the renderer draws from) and `SimClient` (everything that drives or inspects the simulation — trains, control handles, ATO, paths, routes, route-runs). `API implements LayoutDataClient, SimClient`, and `FullDataClient` is their intersection. This is the load-bearing seam of the whole frontend: the editor and renderer talk only to this interface, never to a concrete backend, so a layout-only host implements just `LayoutDataClient`, and swapping the object swaps the backend with the engines unchanged.

### state-manager

> Explain lib/state-manager, the simulation state store the overlay and panels subscribe to

[lib/state-manager.ts](frontend/src/lib/state-manager.ts) is `StateManager` (exported as the singleton `stateManager`). Its header is explicit that it is **not** the source of truth — the backend is — but a cache that mirrors backend state, diffs incoming data, and notifies subscribers only when *their* entity changes. It holds a `Map` per entity type (`trains`, `controllers`, `dispatcherProgress`, `assignedRoutes`, `blocks`, `signals`, `switches`, `stations`, `tracks`); the split of train runtime across three sibling maps mirrors the backend's "Data Flow To UI" contract, where train, controller, and dispatcher state are owned by separate entities and subscribed independently. Consumers subscribe by `type` + `id`; the manager supports pausing/resuming notification batches and a separate clock subscription for `simulationTime`. Each `refreshNetwork()` pushes new data in, and the `SimRendererOverlay` and `train-monitor` are its main subscribers.

### geometry (camera, pathfinders, graph-search)

> Explain lib/camera, pathfinders, and graph-search — the client-side geometry and track-laying helpers

These three modules are the editor's client-side geometry brain. [camera.ts](frontend/src/lib/camera.ts) is the `Camera` — viewport plus isometric projection: it owns pan/zoom, the `gridToScreen` transforms every handler uses, and an `onChange` listener list (the main canvas works in CSS pixels). [pathfinders.ts](frontend/src/lib/pathfinders.ts) is the track-construction engine behind the editor's drag-to-build preview: `ConstructionPathfinder` finds a buildable run between two tiles, `pathToTracks` / `pathsToTrackBits` turn a path into 6-bit tile geometry, and helpers like `tileSkipsSegment`, `getDirection`/`getOpposite`, and `VALID_DETECTION_DIRECTIONS` enforce what can legally connect. [graph-search.ts](frontend/src/lib/graph-search.ts) is `graphSearch<N>` — one generic best-first search that backs every editor search (reachability, connectivity, route tracing) so there is a single, tested traversal rather than many ad-hoc ones.

### helpers

> Explain lib/helpers, toast, popover-anchor, constants, and speed-profile-curve

The remaining `lib/` modules are small, shared utilities. [helpers.ts](frontend/src/lib/helpers.ts) holds reusable UI components and helpers — `showConfirmModal` / `showFormModal`, `renderBitToggle`, `getBlockStateColor`, `formatBlockDirection`, and a set of track-bit / orientation utilities used across the handlers. [toast.ts](frontend/src/lib/toast.ts) is the global notification system (`showToast` / `dismissToast`, error/success/warning/info). [popover-anchor.ts](frontend/src/lib/popover-anchor.ts) provides `anchorToTiles`, which pins a floating element over a set of grid tiles and keeps it aligned through `camera.onChange` — used by the anchored "create block / platform" dialogs. [constants.ts](frontend/src/lib/constants.ts) is the canonical home of the 6-bit track-bit constants (`TRACK_NS`…`TRACK_SW`) and the `BIT_TO_EDGES` / `EDGE_MASKS` / `OPPOSITE_EDGE` tables that mirror the backend tile geometry. [speed-profile-curve.ts](frontend/src/lib/speed-profile-curve.ts) is pure curve math — `interpolate`, `voltageToPower` / `powerToVoltage`, `powerPercentToSpeed` / `speedToPower`, `speedToVoltageTwoStage`, `topSpeed` — mirroring the backend `SpeedProfile` two-stage calibration so the editor's speed-profile UI can preview the curve without a round-trip.

## Chapter 11 — The seam

### Flask backend /api

> Explain how the frontend talks to the Flask backend over /api — requests down, full state snapshots pushed back up

All client↔server traffic is JSON over the `/api` prefix, funneled through the single `API` class. Commands go down as ordinary requests; the network snapshot comes back up on one server-sent event stream. Two directions matter. *Down*, the editor handlers and the `App`'s control buttons issue commands and edits — `POST`/`PATCH`/`DELETE` to create a track, set a cab, depart a train, save a layout — each one a method on the data contract. *Up*, the server pushes the network's live `to_dict` payload (entities enriched with runtime state — block occupancy, signal aspects, switch locks, train spans) on `GET /api/network/stream` every 200 ms and right after any state-changing request, building it once for every open tab (`network_stream.py`). `NetworkStream` (`lib/network-stream.ts`) receives each message and hands it to `NetworkPoller` (`lib/network-poll.ts`), which keeps the layout from the last full message and lays the live fields over it, since the server sends the layout only when it changed; `App.refreshNetwork()` then feeds the result into `stateManager`, and the renderer overlay plus panels redraw from their subscriptions. `refreshNetwork()` with no argument still reads `GET /api/network` once, which edit handlers use after a change and which `NetworkStream` runs every 200 ms while the stream is down. The only coupling across this seam is the `LayoutDataClient` / `SimClient` interface, which is why the same editor and renderer can run against any backend that implements it.

---

*Generated as a structural companion to [README.md](README.md) and [design_new.md](design_new.md). File and line references were accurate at the time of writing; if a symbol has moved, trust the code over this guide.*
