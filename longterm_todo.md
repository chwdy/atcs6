# Long-term TODO

Architectural items shelved from the 2026-05-24 CTO review and the
2026-05-29 follow-up review. Not blocking day-to-day work, but worth
picking up when the codebase has breathing room or when a related change
makes one of them cheap.

## Shelved items

### 1. Add a service layer between Flask routes and the domain

**Status:** Acknowledged, deferred.

**Problem.** Route handlers in `routes/*.py` mutate `state.network.*` and
`state.sim_controller` directly. There is no service module that owns
multi-entity transactional operations. This means:

- A multi-step domain operation (e.g. "delete a layout and stop all
  schedules tied to it") lives inline in the HTTP handler and cannot be
  reused by `demo_main.py`, journey tests, or future CLI tools.
- Unit tests for domain rules require constructing a Flask test client.
- Future cross-cutting concerns (authorization, audit logging,
  transactional rollback) have 40+ retrofit sites instead of one.

**Sketch of the fix.** Introduce a `services/` package alongside `routes/`:

```
services/
    layout_service.py       # save/load/delete layouts; owns the multi-write
    train_route_service.py  # CRUD + activate/deactivate; owns RouteRun lifecycle
    schedule_service.py     # schedule CRUD; coordinates with train routes
    speed_profile_service.py
    switch_service.py
```

Each service takes `state: AppState` (or just the entities it needs) and
exposes pure operations. Route handlers shrink to: parse request → call
service → format response. Tests can call services directly without a
Flask request context.

**When to do it.** When the next feature touches three or more entities
in one request, or when we add authorization. Whichever comes first.

---

### 2. Add fast unit tests for newly extracted helpers (the "wedge" into the god class)

**Status:** Acknowledged, deferred.

**Problem.** The test suite is heavy on journey/scenario tests (~8 of
them) that simulate the full per-tick loop. They are the right tests for
emergent behavior, but a one-line change to a controller helper triggers
a multi-second journey run to know if it broke anything. New helpers
extracted from `TrainController` (see `ATCCalculator`, `ATONavigator`,
`LookaheadBuilder`) currently inherit this slow feedback loop because
they still take a `TrainController` back-reference.

**Sketch of the fix.** Pair every god-class extraction with pure-function
unit tests. Concretely:

1. When extracting a helper, change its signature to take values, not
   the controller (see the ATCCalculator note in the review — pass
   `Block`, `Train.facing_direction`, `SignalState` directly instead of
   `controller._get_*()`).
2. Add `tests/test_<helper>_pure.py` exercising the helper with
   hand-built inputs — no network, no controller, no simulation.
3. Keep the journey tests as the safety net for emergent behavior.

**When to do it.** Alongside (not before) the god-class shrink. The
helper has to be pure-input first; the unit tests come for free after.

---

### 3. Split `design_new.md` by domain before it crosses 3,000 lines

**Status:** Acknowledged, deferred at user discretion.

**Problem.** The design doc is 2,171 lines today and treated as a binding
contract (good). At current growth rate it will cross 3,000 lines within
a year, at which point "no cross-reference drift" becomes hard to enforce
by eye.

**Sketch of the fix.** Split by canonical-home domain — e.g.
`design/controller.md`, `design/path_dispatcher.md`,
`design/persistence.md`, `design/route_executor.md`, with a top-level
`design/README.md` index. Keep the Phase Table and Latch Table at the
top level since they cross-cut every domain.

**When to do it.** User's call. Not urgent. Flag if the doc crosses 3,000
lines or if a single edit starts touching more than four sections.

---

### 4. Move root `.py` files into a `src/atcs6/` package

**Status:** Acknowledged, deferred.

**Problem.** The repo has **51 Python files at the top level** with no
package structure. `network.py`, `path_dispatcher.py`, `route_executor.py`,
`train_physics.py`, `block_manager.py`, demos, server, persistence —
all peers in one directory. There is no way to enforce dependency
direction ("the domain layer must not depend on the dispatcher") when
every file is one `from foo import bar` away from every other. This is
the foundational issue that makes items 5–7 below hard to do cleanly.

**Sketch of the fix.** Move files into a `src/atcs6/` layout along
existing seams:

```
src/atcs6/
    domain/        # block, train, switch, signal, track, station, path — data + invariants
    runtime/       # network, simulation, path_dispatcher, route_executor, train_controller/
    interfaces/    # server, routes/, services/, persistence, app_factory, app_state
    payloads/      # runtime_payloads.py and friends
examples/          # demo_main.py, demo_3.py, demo_4.py (out of the source tree)
```

The move itself is mechanical — mostly `git mv` plus import rewrites.
The value is the boundary it creates: add `import-linter` to CI so that
`domain/` cannot import from `runtime/` or `interfaces/`. Without the
package, every future "thou shalt not" rule is honor-system.

**When to do it.** Before either of items 5 or 6. They are much harder
to do cleanly inside a flat module tree.

---

### 5. Replace `getattr`-based coupling between `PathDispatcher` and `RouteExecutor` with declared `Protocol`s

**Status:** Acknowledged, deferred.

**Problem.** The two largest runtime classes reference each other through
runtime feature detection rather than declared types:

- `route_executor.py:49` — `if TYPE_CHECKING: from path_dispatcher import PathDispatcher` (circular-import workaround).
- `path_dispatcher.py:166` — `_get_route_executor` walks `getattr(sc, "route_executor", None)`.
- `path_dispatcher.py:1294` — `handler = getattr(route_executor, "handle_path_cancellation", None)`.

This is the most critical seam in the system, and it has no compile-time
contract. The dependency direction was never decided — both sides call
each other, and `getattr(..., None)` papers over the resulting circular
references. Any rename or signature change on either side fails
silently; the only safety net is the journey tests.

**Sketch of the fix.** Define two `typing.Protocol`s in `runtime_payloads.py`
(or a new `protocols.py`) that name the surface each side exposes to the
other — e.g. `PathDispatcherForRouteExecutor` with `create_path`,
`extend_path`, `cancel_path`, `active_path_id_by_train`; and
`RouteExecutorForPathDispatcher` with `handle_train_advanced`,
`handle_path_cancellation`, `handle_path_completion`. Inject the
protocols, not the concrete classes. Pick which side is the
orchestrator and which is the resource and make that explicit in the
constructor signatures. `getattr` lookups become typed attribute access
and `mypy` / `pyright` can finally see drift.

**When to do it.** When the next bug at this seam costs more than half a
day to track down. Or alongside item 6 — both touch the same files.

---

### 6. Decompose the god objects (`RailwayNetwork`, `PathDispatcher`, `RouteExecutor`)

**Status:** Acknowledged, deferred.

**Problem.** Three classes own most of the runtime:

| Class | File | Lines | Methods |
|---|---|---|---|
| `RailwayNetwork` | `network.py` | 1,973 | 100 |
| `PathDispatcher` | `path_dispatcher.py` | 2,331 | 49 |
| `RouteExecutor` | `route_executor.py` | 2,074 | 64 |

`RailwayNetwork` alone owns tile lookup, block CRUD, signal placement,
tunnel management, neighbor traversal, propagation, and validation —
six responsibilities sharing one `self`. The existing
`train_controller/` package (atc, ato, eb, manual, ops, physics_sync,
route_hooks) is the shape the rest of the codebase should follow:
small files, one job each, behind a thin facade.

**Sketch of the fix.** Split each god object along the seams that are
already there in spirit:

- `RailwayNetwork` → `TileGrid`, `BlockRegistry`, `SignalRegistry`, `TunnelRegistry`, `NeighborQuery`, all behind a `RailwayNetwork` facade that delegates. The 100 methods become 5 × 15-ish.
- `PathDispatcher` → `PathLifecycle` (create/extend/cancel), `Pathfinder` (BFS + reachable endpoints), `CallbackDispatcher` (Phase-3 fan-out), `RecoveryCoordinator`.
- `RouteExecutor` → `Phase1Planner`, `DwellManager`, `RunArchive`, `RunOrderer`. Phase-3 callbacks remain on the executor or move to `CallbackDispatcher` depending on item 5's direction call.

Do the splits **after** item 4 and **with** item 2 (pair each extraction
with pure-input unit tests). This is months of work; expect to do it
one god object at a time.

**When to do it.** When the next significant feature would have to
modify one of these files. Don't undertake speculatively.

---

### 7. Replace the `AppState` bag with constructor-injected dependencies

**Status:** Acknowledged, deferred.

**Problem.** `app_state.py` defines an `AppState` "dataclass" that bundles
`network`, `path_dispatcher`, `sim_controller`, `persistence_manager`,
`speed_profile_registry`, `simulation_lock` (a `threading.Lock`),
`simulation_running`, `simulation_speed`, `last_update_time`,
`last_autosave_time`. `app_factory.py:33` then stashes it as
`app.extensions["state"] = state` and every route handler reaches in.
This is the service-locator anti-pattern, and the file's own docstring
admits it: `TODO: Move mutable runtime flags to AppState-backed access`.

Concrete symptoms:

- A dataclass that holds a lock and mutable booleans is not a dataclass — it is global mutable state with a polite wrapper.
- Concurrency boundaries are unclear. Flask request threads, the simulation tick thread, and the autosave loop all touch the bag. One lock for everything is either too coarse (kills throughput) or too narrow (races); we cannot tell which by reading the structure.
- Route handlers and services pull whatever they want from `state.*`, so there is no record of which collaborator each handler actually needs.

**Sketch of the fix.** Two passes:

1. Split `AppState` by lifetime: a `Container` of long-lived
   collaborators (network, dispatcher, registries) for constructor
   injection, and a `SimulationRuntime` object that owns the mutable
   tick state (`running`, `speed`, `last_update_time`) plus its own
   lock. Pass these explicitly to services and route modules — no more
   `app.extensions["state"]` lookup.
2. Audit the lock surface. Decide per collaborator whether it needs its
   own lock, whether the sim thread is the only writer, or whether
   immutability + copy-on-read removes the need. Document the answer in
   `docs/`.

**When to do it.** Alongside item 1 (service layer) — the same handlers
are touched by both. Or earlier if a concurrency bug bites.

---

### 8. Add a real frontend build pipeline

**Status:** Acknowledged, deferred.

**Problem.** The frontend is script-tag soup:

- `static/index.html` loads **33 individual `<script>` tags**, no bundler, no ES modules.
- Cache busting is hand-edited query strings (`?v=15`, `?v=18`) in HTML.
- `static/js/editor.js` is **2,806 lines** of global-window code; `editor-routes.js` is 2,435; `editor-trains.js` is 1,887. These are the JS twins of the Python god objects.
- No TypeScript. The Python ↔ JS contract is enforced by hope, and any payload field rename on the server side breaks the client silently.
- No frontend tests in `static/js/`. The Python side has 100 test files; the frontend has zero.

**Sketch of the fix.** Smallest credible step:

1. Adopt Vite (or esbuild) + TypeScript + ES modules. One bundle output, automatic content-hashed filenames, dev server with hot reload. `static/index.html` becomes a stub that loads one entry point.
2. Convert one module per week, starting with the renderer family (they have the cleanest interfaces). Editor modules go last — they share most state.
3. Generate TypeScript types from the Python payload schemas (`runtime_payloads.py`). `pydantic` → JSON Schema → `json-schema-to-typescript` is one straightforward path. This kills the silent-rename problem.
4. Add Vitest, port the most behavior-critical helpers (`pathfinders.js`, `helpers.js`, `camera.js`) to unit tests first.

**When to do it.** When the next non-trivial frontend feature would
push `editor.js` over 3,000 lines, or when the first JS-vs-Python
field-name bug bites in production.

---

### 9. Version the HTTP API

**Status:** Acknowledged, deferred.

**Problem.** Endpoints in `routes/*.py` are flat and unversioned:
`/api/network`, `/api/control`, `/api/trains`, etc. The frontend is the
only consumer today, so the cost of an incompatible change is "edit
both sides at once." That stops being true the first time anything else
talks to the server — a CLI, a journey-test fixture, an external tool,
or a future agent.

**Sketch of the fix.** Add a `/api/v1/` prefix to every existing route
and update the client. Reserve `/api/` (unversioned) for endpoints we
explicitly want to remain unstable (debug, health). Document the
versioning policy in `docs/API_CONVENTIONS.md`.

**When to do it.** Before the second API consumer ships. Cheap now,
expensive later.

---

### 10. Minor hygiene

**Status:** Acknowledged, deferred.

A list of small structural smells worth a sweep on a slow afternoon:

- `demo_main.py`, `demo_3.py`, `demo_4.py` sit at the repo root next to production code. Move to `examples/` or `scenarios/`.
- `TODO List:` blocks embedded inside module docstrings (e.g. `app_state.py`, `app_factory.py`). Process artifacts belong in this file, not in source.
- `__pycache__/` appears in the working tree listing — confirm it is `.gitignore`d and not tracked.
- Replace ad-hoc `Optional[object]` parameters (e.g. `route_executor.py:171`) with proper forward references or protocols.
- `routes/*.py` has 17 modules but no shared request-parsing helper. The next route added that does JSON validation is the prompt to introduce one (likely `pydantic` request models, dovetailing with item 8's payload-types work).

**When to do it.** Whenever you are already in the area for another
reason.

---

*Last reviewed: 2026-05-29*
