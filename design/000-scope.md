> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

# ATCS6 Desired Design Model

This document defines the target refactor contracts and changed cross-component behavior for the current ATCS6 codebase.
It is intended to guide an engineer refactoring the existing implementation, not to replace the code as a complete standalone implementation spec.
Unchanged entity-local fields, helper internals, and serialization details continue to follow the current code unless this document states otherwise.
If current code conflicts with a changed contract stated here, this document wins.

## About this document

This is a design document used by AI agents (and human reviewers) to write code for a train control simulator. It describes the v1 system as it should be built, not the history of how it got here. Read with these constraints in mind:

- **`TrainController` mirrors a real-world train computer**, which is a multi-module unit (ATC, ATO, EB, cab interface, mode manager, operational state). Its breadth is intentional domain modeling. Sub-modules are discrete concerns even when adjacent prose mentions several together.
- **Latches model hardware that cannot release work mid-motion.** A latch is a deferred command that persists in controller-owned state until a gating predicate (typically `is_stopped()`) lets it commit. A flag is not a latch. Adding a new latch requires identifying the gating predicate first.
- **The runtime is single-threaded.** All API, UI, and admin requests run between ticks on the same thread as the per-tick phase loop. Every "always recompute live" rule in this document depends on this; breaking the threading model invalidates several core design choices.
- **v1 means v1.** Anything not in v1 must not appear in this document, even as "removed," "legacy," or "deprecated" prose. The absence of a row in a schema table is itself the statement that the thing does not exist. Use git for design history.
- **Performance has been validated for the v1 scope.** Compute-live patterns (`lookahead`, `received_path_plan` projection, derived properties) are intentional, not performance bugs to optimize.
- **Tests are out of scope for this document.** It defines contracts that code generation must satisfy. Test plans live elsewhere.
- **The doc iterates fast.** When a design changes, every cross-reference is swept in the same edit. Readers never see a partial update; if you find one, that is a bug, not history.

## Authoring Rules

These rules govern every edit to this document. Reviewers must reject any patch that violates them.

1. **This document is v1. There is no "removed in v1," "legacy," "deprecated," or "no longer used" prose anywhere in this file.** If a field, method, state, or flag is not part of v1 it must not appear here at all. The absence of a row in a schema table is itself the statement that the thing does not exist. Any patch that introduces tombstone language ("removed in v1," "no longer used," "kept for compatibility," "deprecated," etc.) is incomplete and must be rejected.

2. **No cross-reference drift.** When any rule changes — ownership, payload field, state transition, callback signature, latch consumer, recovery gate — every site in the document that references the old rule must be swept and updated in the same edit. A patch that updates only one section is incomplete. Reviewers must reject any patch that leaves divergent restatements of the same rule anywhere in the document.

3. **One canonical home per concept.** Each shared schema, latch, predicate, and helper has exactly one canonical definition site. All other sites reference it by name. If two sites describe the same concept differently, the patch is incomplete.

4. **Latches are described in the canonical Latch Table.** Every latch in the document must appear in the Latch Table (in the Core Runtime Loop section) with: writer phase + entity, consumer phase + entity, the "consumed by the last entity that uses it in the same 3-phase period" rule, and any multi-tick survival conditions. Latch behavior described elsewhere must cross-reference the table, not restate its rules.

5. **Phase boundaries are described in the canonical Phase Table.** The Phase Table (in the Core Runtime Loop section) is the only place that defines which entity acts in which phase. All other sections cross-reference it by phase name and entity.

6. **No single-use-case branches in shared contracts.** If a workflow step is written as a per-case branch, it must either be expressed as a general mechanism or explicitly justified as the only case the general mechanism cannot cover.

7. **Diagnostic-only distinctions belong on events, not enums.** Distinctions that have no functional consumer must be expressed through `NotificationCenter` event payload, not by adding enum values to control-flow types. Enum values that are written by structurally different code paths (and could plausibly become functional) may stay; enum values that exist purely so the UI can render a different label must be moved to event payload.

## Scope

Primary files reviewed:

- `simulation.py`
- `train.py`
- `train_controller/` (package: `__init__.py` façade plus `atc.py`, `ato.py`, `commands.py`, `eb.py`, `manual.py`, `notifications.py`, `ops.py`, `path_projection.py`, `physics_sync.py`, `route_hooks.py`)
- `train_physics.py`
- `railway_platform.py`
- `station.py`
- `block.py`
- `block_manager.py`
- `path.py`
- `path_dispatcher.py`
- `route_executor.py`
- `train_route.py`
- `track.py`
- `switch.py`
- `network.py`
- `speed_profile.py`
- `block_power.py`

Terminology:

- `path`: a dispatcher-managed reserved movement authority between blocks.
- `route`: a higher-level travel plan made of `stop` / `waypoint` / `dwell` tasks that ultimately resolve to concrete block targets.
- `route run`: the per-train runtime instance of an assigned route. It references one shared `Route` definition but owns that train's mutable execution state: waypoint cursors, the current resolved target status, dwell timing, and active-path correlation. Current-code implementation may still keep the class name `RouteExecutionState`, but the canonical concept is `RouteRun`.
- `block`: an occupancy and reservation unit.
- `track tile` / `TrackSegment`: one physical rail tile on the grid, identified by its `(x, y)` coordinate and local 6-bit geometry. Despite the legacy class name, a `TrackSegment` is not a collection of tiles and is not a block; a `Block` owns an ordered collection of these tile IDs.
- `platform`: the concrete physical stopping place on the track. Dispatcher targets are always expressed against a concrete block traversal frame, never against a logical station group; when a waypoint resolves through station/platform data, the platform supplies the linked blocks and traversal context used to derive that block target.
- `station`: a logical grouping of one or more platforms used only for route-level stop selection. When a route targets a station-level stop, `RouteExecutor` must first resolve one concrete platform for that attempt and then derive the dispatcher request contract from that resolved platform.

## Out of Scope

This section lists features and concerns that v1 deliberately does not include. Its purpose is to make absences explicit so that readers (AI agents and humans) can confirm an absence is a decision rather than an oversight.

A separate Out of Scope entry is not a wishlist and not a promise to implement later. It records that the question was raised and answered "no" for v1. Three discipline rules govern what goes here:

1. **Out of Scope is an explicit "no," not a brainstorm.** An entry only goes here when a designer has actively decided not to include a concern for v1. If the question has not been raised, do not speculate.
2. **Out of Scope is not a graveyard.** Concerns that were once in scope and got removed do not belong here; they go nowhere, per the "removed is removed" rule in the Authoring Rules. Only never-was concerns belong.
3. **Out of Scope does not promise v2.** Listing something here is not a roadmap. v2 will have its own scope conversation.

| Concern | v1 stance | Why Out of Scope for v1 |
| --- | --- | --- |
| Multi-threaded runtime | Single thread; all API/UI/admin requests run between ticks on the same thread as the per-tick phase loop | The "always recompute live" projections (`lookahead`, `received_path_plan`, derived properties) depend on between-tick mutation only. Adding threads invalidates the read-consistency contract. |
| Lookahead caching / projection caching | Recompute live on every read | Performance is acceptable for v1 train counts. Caching adds invalidation surface that is not needed. |
| Persistence / save-load of runtime state | In-memory only | No use case in v1. |
| Schema migration, backward-compat shims, dual-write transitional fields | None | v1 is the first design; "v1 means v1." |
| Hardware fault states (switch stuck mid-throw, signal failures, EB fails to engage) | Not modeled; happy hardware assumed | Fault simulation is its own design problem. |
| Collision modeling | Two trains in one block is a fatal runtime failure via `SimulationController.enter_failed_runtime(...)`, not a physics interaction | Physics does not model contact. |
| Reverse motion under cab | Forbidden; wrong-direction cab forces the distance-0 ATC limit to `0` | Physics never produces backward motion. |
| Authorization / RBAC / multi-user model | All API requests trusted | No user accounts in v1. |
| Logging beyond `NotificationCenter` | `NotificationCenter` is the only runtime sink | Component-local logging is forbidden by design. |
| Idempotency keys, retry/dedup wrappers on API commands | None | Single-threaded between-tick drain makes them unnecessary. |
| Health checks, metrics export, distributed tracing | None | Operators read `NotificationCenter` and live projections. |
| `Path` / `RouteRun` / notification retention caps | Unbounded for v1 | Retention policy will be added when leak becomes measurable. |
| Per-train safety margin tuning | Two global constants (`TRACK_END_SAFETY_MARGIN_CM`, `PLATFORM_SAFETY_MARGIN_CM`) | Per-train tuning is a future calibration concern. |
| Per-segment speed limits beyond `Block.speed_limit_kmh` | Block-scoped only | Sub-block speed restrictions are not modeled. |
| Live editor mutation during simulation | Forbidden; editor and runtime are binary modes per session | Geometry stability is required by every "always recompute live" rule. |
| Multi-network in one process | One network per `SimulationController` instance | Cross-network interaction has no use case in v1. |
| Replay / time-travel debugging | None | Use `NotificationCenter` monotonic ordering for after-the-fact reconstruction. |
| Schedule adherence / ETA / SLA tracking | None | `RouteTaskRun.started_at` / `finished_at` are informational only; control logic does not read them. |
| Pluggable physics or speed-profile backends | None | `SpeedProfile` is the single calibration model. |
| Configuration files, env vars, runtime tunable injection | None | Globals are constants in code. |
| Async/streaming notification protocols (websocket, SSE, push) | None | Frontend reads `NotificationCenter` via polling/projection. |
| Cross-tick latch durability across process restart | None | Process state is volatile. |
| Watchdogs / alert notifications for stuck trains, no-progress | None | Operators read live diagnostics from `lookahead.speed_curves[0].atc_limit_reason_code` and `RouteRun.last_error`. |
| Train removal contract | Pending design; do not implement removal until specified | Surface, teardown order, and pending-latch handling are not yet decided. |
| Bulk operations (multi-train commands, batch route assignment) | None | Operators issue per-train commands one at a time. |
