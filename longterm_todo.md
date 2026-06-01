# Long-term TODO

Architectural items shelved from the 2026-05-24 CTO review and the
2026-05-29 follow-up review. Not blocking day-to-day work, but worth
picking up when the codebase has breathing room or when a related change
makes one of them cheap. Completed items are removed from this list as they
land — see git history.

## Open items

### 1. Add a service layer between Flask routes and the domain

**Status:** In progress. The `services/` package exists and the five
services originally sketched here are built and unit-tested; what remains
is migrating the other route modules onto the same pattern.

**Problem.** Route handlers in `routes/*.py` mutate `state.network.*` and
`state.sim_controller` directly. Without a service module that owns
multi-entity operations:

- A multi-step domain operation (e.g. "delete a layout and stop all
  schedules tied to it") lives inline in the HTTP handler and cannot be
  reused by `demo_main.py`, journey tests, or future CLI tools.
- Unit tests for domain rules require constructing a Flask test client.
- Future cross-cutting concerns (authorization, audit logging,
  transactional rollback) have 40+ retrofit sites instead of one.

**The pattern (now established).** `services/` sits alongside `routes/`.
Each service function takes `state: AppState` plus structured kwargs,
returns domain objects, and raises a typed `ServiceError` subclass on
failure (`ValidationError` → 400, `NotFoundError` → 404, `ConflictError`
→ 409, `ServiceUnavailableError` → 503). Route handlers shrink to: parse
request → call service → translate exceptions + serialize. Non-HTTP
callers invoke the same functions directly — see `tests/test_services.py`.

**Built so far** (service + thin route, both covered by `test_services.py`):

```
services/
    exceptions.py            # the typed ServiceError hierarchy
    layout_service.py        # save/load/delete; owns the pause-and-lock multi-write
    train_route_service.py   # route CRUD + activate/deactivate; RouteRun lifecycle
    schedule_service.py      # schedule CRUD; coordinates with train routes
    speed_profile_service.py # profile CRUD; owns simulation_lock + default/in-use guards
    switch_service.py        # switch CRUD + availability flags + SwitchManager position changes
    train_service.py         # train CRUD + placement + route assignment + operator commands
    block_service.py         # block CRUD + availability flags + track membership + can_extend
    track_service.py         # track tile CRUD + bulk add/delete + orphan-impact pre-check
    platform_service.py      # platform CRUD + tile membership + placement predicates
```

Migrated routes: `layouts`, `schedules`, `train_routes`, `speed_profiles`,
`switches`, `trains`, `blocks`, `tracks`, `platforms`.

**Remaining.** `signals`, `stations`, `paths`, `route_runs`, `tunnels`,
`inspector`, `simulation` still hold their domain logic inline. Adopt the
same pattern when one is next touched; `paths` is heavily
`PathDispatcher`-coupled (better paired with item 5), and some
(`inspector`, much of `simulation`) are largely read-only and lower-value
to migrate.

**When to do it.** Incrementally, when the next feature touches a given
route — or eagerly for `blocks`/`trains` if they grow further. The pattern
and its test harness are in place, so each migration is now a small,
self-contained slice.

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

**When to do it.** Alongside (not before) the god-class shrink (item 5). The
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
the foundational issue that makes items 5 and 6 below hard to do cleanly.

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

**When to do it.** Before item 5 (decomposing the god objects). It is much
harder to do cleanly inside a flat module tree.

---

### 5. Decompose the god objects (`RailwayNetwork`, `PathDispatcher`, `RouteExecutor`)

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
- `RouteExecutor` → `Phase1Planner`, `DwellManager`, `RunArchive`, `RunOrderer`. The dispatcher↔executor seam direction is already settled by the typed Protocols in `runtime_payloads.py`: `PathDispatcher` is the *resource* `RouteExecutor` drives, and `RouteExecutor` is the Phase-3 *callback observer* `PathDispatcher` calls back into — so the Phase-3 callbacks stay on the executor unless a dedicated `CallbackDispatcher` is extracted.

Do the splits **after** item 4 and **with** item 2 (pair each extraction
with pure-input unit tests). This is months of work; expect to do it
one god object at a time.

**When to do it.** When the next significant feature would have to
modify one of these files. Don't undertake speculatively.

---

### 6. Replace the `AppState` bag with constructor-injected dependencies

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

### 7. Version the HTTP API

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

### 8. Minor hygiene

**Status:** Acknowledged, deferred.

A list of small structural smells worth a sweep on a slow afternoon:

- `demo_main.py`, `demo_3.py`, `demo_4.py` sit at the repo root next to production code. Move to `examples/` or `scenarios/`.
- `TODO List:` blocks embedded inside module docstrings (e.g. `app_state.py`, `app_factory.py`). Process artifacts belong in this file, not in source.
- `__pycache__/` appears in the working tree listing — confirm it is `.gitignore`d and not tracked.
- Replace ad-hoc `Optional[object]` parameters (e.g. `route_executor.py:171`) with proper forward references or protocols.
- `routes/*.py` has 17 modules but no shared request-parsing helper. The next route added that does JSON validation is the prompt to introduce one (likely `pydantic` request models, dovetailing with the payload-types tooling (`tools/gen_payload_types.py` -> `frontend/src/payloads.generated.ts`) from the now-landed frontend build pipeline).

**When to do it.** Whenever you are already in the area for another
reason.

---

*Last updated: 2026-06-01*
