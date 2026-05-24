# Long-term TODO

Architectural items shelved from the 2026-05-24 CTO review. Not blocking
day-to-day work, but worth picking up when the codebase has breathing room
or when a related change makes one of them cheap.

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

*Last reviewed: 2026-05-24*
