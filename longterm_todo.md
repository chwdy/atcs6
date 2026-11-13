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

**Problem.** Route handlers in `routes/*.py` mutate `state.network.*`
directly. Without a service module that owns
multi-entity operations:

- A multi-step domain operation lives inline in the HTTP handler and
  cannot be reused by tests or future CLI tools.
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
    layout_service.py        # save/load/delete
    switch_service.py        # switch CRUD + availability flags + position changes
    block_service.py         # block CRUD + availability flags + track membership
    segment_service.py       # segment CRUD
    track_service.py         # track tile CRUD + bulk add/delete + orphan-impact pre-check
```

**Remaining.** Route modules in `routes/` that still hold their domain
logic inline (`tunnels`, `simulation`, `hardware`, ...). Adopt the same
pattern when one is next touched.

**When to do it.** Incrementally, when the next feature touches a given
route — or eagerly for `blocks` if it grows further. The pattern
and its test harness are in place, so each migration is now a small,
self-contained slice.

---

### 4. Move root `.py` files into a `src/atcs6/` package

**Status:** Acknowledged, deferred.

**Problem.** The repo has **42 Python files at the top level** with no
package structure. `network.py`, `simulation.py`, `runtime.py`, `block_power.py`,
demo scenes, server, persistence —
all peers in one directory. There is no way to enforce dependency
direction ("the domain layer must not depend on the runtime") when
every file is one `from foo import bar` away from every other. This is
the foundational issue that makes items 5 and 6 below hard to do cleanly.

**Sketch of the fix.** Move files into a `src/atcs6/` layout along
existing seams:

```
src/atcs6/
    domain/        # block, segment, switch, track, tunnel — data + invariants
    runtime/       # network, simulation, runtime, block_occupancy
    interfaces/    # server, routes/, services/, persistence, app_factory, app_state
    payloads/      # runtime_payloads.py and friends
examples/          # demo7_1.py, demo7_2.py (out of the source tree)
```

The move itself is mechanical — mostly `git mv` plus import rewrites.
The value is the boundary it creates: add `import-linter` to CI so that
`domain/` cannot import from `runtime/` or `interfaces/`. Without the
package, every future "thou shalt not" rule is honor-system.

**When to do it.** Before item 5 (decomposing the god objects). It is much
harder to do cleanly inside a flat module tree.

---

### 5. Decompose the god object (`RailwayNetwork`)

**Status:** Acknowledged, deferred.

**Problem.** `RailwayNetwork` (`network.py`, 1,200 lines, 64 methods) owns
tile lookup, block CRUD, tunnel management, neighbor traversal,
propagation and validation, all sharing one `self`.

**Sketch of the fix.** Split it along the seams that are already there:
`TileGrid`, `BlockRegistry`, `TunnelRegistry`, `NeighborQuery`, all
behind a `RailwayNetwork` facade that delegates. Pair each extraction
with unit tests that build their inputs by hand.

Do the split **after** item 4.

**When to do it.** When the next significant feature would have to
modify `network.py`. Don't undertake speculatively.

---

### 6. Replace the `AppState` bag with constructor-injected dependencies

**Status:** Acknowledged, deferred.

**Problem.** `app_state.py` defines an `AppState` "dataclass" that bundles
`network`, `sim_controller`, `persistence_manager`, `simulation_lock`
(a `threading.Lock`), `last_autosave_time`, `operating_time` and
`setup_active`. `app_factory.py:33` then stashes it as
`app.extensions["state"] = state` and every route handler reaches in.
This is the service-locator anti-pattern, and the file's own docstring
admits it: `TODO: Move mutable runtime flags to AppState-backed access`.

Concrete symptoms:

- A dataclass that holds a lock and mutable booleans is not a dataclass — it is global mutable state with a polite wrapper.
- Concurrency boundaries are unclear. Flask request threads, the simulation tick thread, and the autosave loop all touch the bag. One lock for everything is either too coarse (kills throughput) or too narrow (races); we cannot tell which by reading the structure.
- Route handlers and services pull whatever they want from `state.*`, so there is no record of which collaborator each handler actually needs.

**Sketch of the fix.** Two passes:

1. Split `AppState` by lifetime: a `Container` of long-lived
   collaborators (network, simulation controller, persistence) for constructor
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
`/api/network`, `/api/blocks`, `/api/hardware`, etc. The frontend is the
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

- The demo scenes (`demo7_1.py`, `demo7_2.py`) sit at the repo root next to production code. Move to `examples/` or `scenarios/`.
- `TODO List:` blocks embedded inside module docstrings (e.g. `app_state.py`, `app_factory.py`). Process artifacts belong in this file, not in source.
- `__pycache__/` appears in the working tree listing — confirm it is `.gitignore`d and not tracked.
- `routes/*.py` has 12 modules but no shared request-parsing helper. The next route added that does JSON validation is the prompt to introduce one (likely `pydantic` request models, dovetailing with the payload-types tooling (`tools/gen_payload_types.py` -> `frontend/src/payloads.generated.ts`) from the now-landed frontend build pipeline).

**When to do it.** Whenever you are already in the area for another
reason.

---

*Last updated: 2026-06-01*
