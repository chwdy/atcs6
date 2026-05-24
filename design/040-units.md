> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Units

**Units contract**

- The unit-mixing hazard between `Train.max_braking_rate` (`m/s^2`) and `SpeedProfile.deceleration_rate` (`km/h/s`) is resolved at exactly one boundary: `TrainPhysics` performs the `m/s^2` ↔ `km/h/s` conversion when it reads profile rates, so all controller-facing deceleration math stays in a single unit system. No other layer reinterprets the raw fields.
- The desired model uses one canonical stopped predicate everywhere: `TrainController.is_stopped()` with `STOPPED_SPEED_THRESHOLD_KMH = 0.1`, reading controller-owned `current_speed_kmh`. `STOPPED_SPEED_THRESHOLD_KMH` is the global "considered stopped" speed threshold; it is distinct from any ATC zero-speed limit. The "ATC limit" mentioned elsewhere in this document means the ATC-derived speed cap at distance 0 in the canonical lookahead — when that cap is `0` the ATC is granting no clearance distance. Controller-reported stop completion, `TrainController.commands.power_off_cmd()`, `TrainController.commands.set_cab()`, `TrainController.commands.set_mode()`, and `TrainController.eb.release()` must all use this same `STOPPED_SPEED_THRESHOLD_KMH` helper instead of introducing layer-specific stop thresholds. Same-block or zero-movement movement, if retained for explicit local/manual repositioning, may complete only when the same tick's post-physics controller stop check says the train is stopped at the usable end of live authority and Phase 3 then calls `PathDispatcher.try_complete_active_path(...)`; dispatcher must not add a separate position-based completion predicate.

**Time sampling**

- v1 timestamps are sampled at the write site, never threaded as method or callback parameters. Whenever this document says `current_time` for a path/route/notification timestamp, it means `time.time()` sampled at the moment the writer commits the write. Every site that records a `last_status_updated_time`, `started_at`, `finished_at`, `last_error.time`, or notification `event_time` calls `time.time()` itself at the moment of the write; callers do not pass the timestamp in. When one writer needs to record both an event time on a notification and a status timestamp on a related entity in the same step, the writer samples `time.time()` once locally and uses that one value for both writes.

**Safety margin globals**

**Why this shape.** Two values, not one, because the right standoff distance from a track-end (where overrun has no platform interaction) is different from the right standoff distance at a platform stop (where overrun affects passenger boarding). One global per terminus type captures the operational distinction without forcing each route or block to author its own value. Putting both values inside `TrainController` lookahead computation gives one canonical projection consumer for ATC, ATO, and stop-completion to read; other layers consume the projected boundary rather than the raw margin number.

- The controller safety margin is two distinct global values, not a single tunable. Both are owned by `TrainController` lookahead computation and are not authored on routes, paths, or per-block fields.
  - `TRACK_END_SAFETY_MARGIN_CM`: applied when the authority-end block is bounded by a track-end / dead-end / non-station authority terminus.
  - `PLATFORM_SAFETY_MARGIN_CM`: applied when the authority-end block is the resolved terminal block of a platform stop.
- `TrainController` lookahead is the single computation site for these margins. When building the authority-boundary projection in `get_lookahead()`, the controller finds the terminal block entry for `authority_end_block_id`, projects the authority-end block's forward boundary in that directed terminal frame, and counts back from that edge by the selected safety margin to build the ATC/ATO speed curve. ATC is intentionally not trying to drive the train all the way to the physical edge of the cleared/authorized block. This projection is a braking and diagnostics calculation only; it is not route-authored stop-position data and is not a route completion tolerance. Other layers (dispatcher, route, physics, signals) must not re-derive or duplicate this margin; they consume the lookahead-projected boundary instead.

## Canonical Entity Index

**Entity list**

- `TrackSegment`
- `TrackResource` (shared identity base of `Block` and `Switch`)
- `Switch`
- `SwitchManager`
- `Block`
- `BlockManager`
- `ResourceCoordinator`
- `Path`
- `PathDispatcher`
- `Route`
- `RouteRun` (current-code implementation may still use the class name `RouteExecutionState`, but the canonical concept is the per-train route run)
- `Platform`
- `Station`
- `Train`
- `TrainController`
- `TrainPhysics`
- `SpeedProfile`

**Canonical shared contracts**

- `ControllerProgressReport` schema is defined once under the **ControllerProgressReport schema** heading in the **Dispatcher progress/completion flow** section. Every producer and consumer refers back to that heading by name.
- `Path` field schema is defined once in the Path section and every producer/consumer should refer back to it by name instead of reconstructing the field set piecemeal.
- `Path.path_tiles` tile-entry schema is defined once in the Path section and every controller/UI/physics geometry projection should refer back to it by name.
- `Path.resource_sequence` entry schema is defined once in the Path section and every controller/UI/physics projection should refer back to it by name.
- `Platform` / `Station` ownership and `RailwayNetwork` reverse-index contracts are defined once in the Platform And Station section and every route/planning/lookup caller should refer back to that section by name.
- `TrainController.ops.state` state-machine table is defined once in the TrainController section and every caller should use that same state set and transition meaning.
- `TrainController.path_projection.received_path_plan` schema is defined once in the TrainController section and every producer/consumer should refer back to it by name.
- `RouteExecutor` path-event callback schema (`RouteExecutor.handle_path_completion(...)` / `RouteExecutor.handle_path_cancellation(...)`) is defined once in the Route section and every callback chain should refer back to it by name.
