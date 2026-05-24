# ATCS6 design specification

This is the canonical design document for the ATCS6 train-simulation
codebase. It covers the per-train tick model, latch discipline, every
domain entity (Block, Switch, Signal, Path, Route, Train, TrainController,
TrainPhysics), and end-to-end ATO flows.

Cross-cutting concerns (the Phase Table, the Latch Table, the Units
contract, the Glossary) live in their own files and are referenced from
every domain doc.

## Reading order

Files are prefixed with 3-digit numbers grouping by domain:

- `0xx` — cross-cutting: scope, glossary, design direction, runtime loop, units
- `1xx` — physical infrastructure entities
- `2xx` — planning / dispatching
- `3xx` — train, controller, physics
- `4xx` — end-to-end flows
- `9xx` — checklists

## Index

| File | Covers |
|---|---|
| [`000-scope.md`](000-scope.md) | Scope, authoring rules, out-of-scope items |
| [`010-glossary.md`](010-glossary.md) | Vocabulary used throughout the spec |
| [`020-direction.md`](020-direction.md) | Design direction and layered architecture |
| [`030-core-loop.md`](030-core-loop.md) | Per-train tick transaction; Phase Table; Latch Table |
| [`040-units.md`](040-units.md) | Units, time sampling, canonical entity index |
| [`100-track-resource.md`](100-track-resource.md) | TrackResource and TrackSegment |
| [`120-switch.md`](120-switch.md) | Switch entity, flank protection, SwitchManager |
| [`130-platform-station.md`](130-platform-station.md) | Platform and station entities |
| [`110-block.md`](110-block.md) | Block entity and BlockManager |
| [`220-resource-coordinator.md`](220-resource-coordinator.md) | ResourceCoordinator |
| [`200-path-dispatcher.md`](200-path-dispatcher.md) | Path and PathDispatcher |
| [`210-route-executor.md`](210-route-executor.md) | Route, RouteRun, RouteExecutor |
| [`300-train.md`](300-train.md) | Train entity |
| [`310-train-controller.md`](310-train-controller.md) | TrainController and its 10 sub-modules |
| [`320-train-physics.md`](320-train-physics.md) | TrainPhysics |
| [`330-speed-profile.md`](330-speed-profile.md) | SpeedProfile |
| [`140-signal.md`](140-signal.md) | Signals |
| [`150-power.md`](150-power.md) | Power |
| [`400-data-flow-ui.md`](400-data-flow-ui.md) | Data flow to the UI |
| [`410-end-to-end.md`](410-end-to-end.md) | End-to-end ATO path run; EB workflow state machine |
| [`900-checklist.md`](900-checklist.md) | Logic review checklist |

## Cross-cutting references

These files are referenced from every domain doc:

- [`030-core-loop.md`](030-core-loop.md) — Phase Table, Latch Table,
  per-train tick transaction, Phase 1/2/3 ownership rules
- [`040-units.md`](040-units.md) — units, time sampling, canonical entity index
- [`010-glossary.md`](010-glossary.md) — vocabulary used throughout the spec

## Authoring rules

This spec is treated as a binding contract per `CLAUDE.md`. Any code
change that violates a rule here, or that introduces vocabulary not
defined here, is incomplete. See [`000-scope.md`](000-scope.md) for the
full authoring-rules block.

## Comparison to the legacy single file

The original single-file spec is preserved at `../design_new.md`
(~2,173 lines / ~73,000 words). The split here is a literal partition
of that content — no rewrites, only the addition of navigation banners
and a README index. Total word count of the split files matches the
original within a few hundred words (delta = nav banners + this README).
