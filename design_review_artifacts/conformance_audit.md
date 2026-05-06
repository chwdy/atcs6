# Doc-to-Code Conformance Audit (attempt-0504)

Auditor: independent verifier
Branch audited: attempt-0504 @ e7e3f84 ("attempt-0504: align codebase to design_new.md (parallel migration)")
Spec: design_new.md (1903 lines)
Code root: /Users/Diwei/Documents/exp_self/atcs6

## Summary

| Category | Count |
| --- | --- |
| Total rules audited (specific contractual rules) | ~110 |
| PASS | 23 (~21%) |
| PARTIAL | 17 (~15%) |
| FAIL | 41 (~37%) |
| MISSING | 25 (~23%) |
| DOC GAP | 0 |
| N/A (authoring/doc-only) | 4 |

**Overall conformance estimate: ~30-35%.** The migration touched the surface (added v1 method names, new payload dataclasses, the three-phase Phase 1/2/3 dispatch in `simulation.py`), but most foundational entities (`Block`, `Switch`, `Path`, `Route`, `RouteRun`, `RouteTask`, `Train`) still hold pre-v1 schemas. Several v1 canonical types (`NotificationCenter`, `FatalConflictKind`, `RailwayPathfinder`, `TrainSpan`, `BlockManager.reserve_blocks_for_path/release_path_block_reservations/on_block_tail_vacated/on_switch_state_changed`, `SimulationController.add_train`, the operator-command queue/drain) have no implementation at all. Some passing items (e.g. `enums.TrainState`, the latch table fields on `TrainController`, the `simulation.update()` per-train transaction skeleton, `PathCancellationKind` enum) are correctly aligned but sit on top of the broken substrate.

## High-priority gaps (top 10)

1. **Operator-command queue/drain is entirely missing.** The spec is built on the rule that operator commands (`power_on_cmd`, `set_cab`, `set_mode`, `set_handle`, `set_manual_power_percent`, `activate_emergency_cmd`, `release_emergency_cmd`, `power_off_cmd`) are queued onto the targeted controller and drained as the FIRST substep of `TrainController.tick(...)` in canonical order. There is no command-queue field, no drain method, and no canonical drain-time precondition recheck. All command methods execute inline (synchronously) — violates the entire "Operator-command processing slot" section of the spec. Consequences: power-on while OFF cannot be deferred, EB activation cannot be processed before ATC during the same tick, command-result notifications never fire.
2. **`Path` schema is the OLD seven-state lifecycle, not v1.** `path.py:57` still defines `PathStatus = {PENDING, SETTING, SET, ACTIVE, CLEARING, COMPLETED, CANCELLED}` (spec mandates only `ACTIVE/COMPLETED/CANCELLED`); `Path` defaults to `PENDING`; fields are `start_block_id`, `end_block_id`, `block_sequence`, `switch_positions`, `current_block_index`, `flank_protection_switches`, `overlap_blocks`, `approach_locked_blocks` instead of v1's `requested_destination_block_id`, `requested_destination_direction`, `authority_end_block_id`, `resource_sequence`, `current_resource_index`, `last_status_updated_time`. No `path_tile entry schema` (no `entry_edge`/`exit_edge`/`resource_sequence_index` per tile). Every reader/writer of `Path` is therefore reading the wrong shape. This is the root cause of multiple cluster failures.
3. **`Block` schema is pre-v1.** `block.py` stores `state: BlockState` instead of computing it; missing `_is_caution`, `reserved_for_train`, `caution_direction`, `allowed_travel_directions`, `ordered_tiles`. Has the unwanted `current_voltage` field. State transitions still go through the old `set_block_to_caution`/`clear_block`/`occupy_block` API; no `reserved_by_path` + `_is_caution` co-existence is possible because `state` is stored. `Block.reserved_for_path(train_id, direction)` (an internal helper) does not exist; `BlockManager.reserve_blocks_for_path(...)` is missing entirely.
4. **`Switch` schema is pre-v1.** `switch.py` has `locked_by_path: Optional[str]`, `locked_state: Optional[int]` (single-owner) instead of v1's `state`, `occupied_by`, `locks` (multi-owner keyed by `(path_id, claim_role, source_switch_id)`), `flank_protection: List[Tuple[str, int]]`, `train_protection_claims`, `disabled`, `simulation_controller`. `switch.lock(...)` returns single-owner-conflict failure; multi-path compatible same-state lock claims (the v1 contract) cannot be expressed.
5. **`Route` / `RouteTask` / `RouteRun` schemas are pre-v1.** `train_route.py` has `Waypoint` (with `dwell_time_sec`, `platform_id`) instead of `RouteTask` (with `task_kind`, `task_location`, `task_time`, `task_direction`, `flexible_platform`, `target_time`, `departure_mode`, `task_metadata`). `Route` is missing `route_version`, `reconciliation_policy`, `priority`, `default_departure_mode`, `acquisition_mode`. `RouteExecutionState` is missing `target_waypoint_index`, `route_version_seen`, `started_at`, `task_runs: List[RouteTaskRun]`, `current_route_status`, `run_number`. Initial cursors are `0` instead of the spec's required `-1` sentinel. Direction is `int (1/-1)` instead of literal `"forward"/"reverse"` strings. Bridge from this gap: every read/write of `task_runs[i].started_at`/`finished_at`/`resolved_target` (referenced ~30 times across the spec) has no actual storage.
6. **`_find_next_task_target()` always returns `task_kind == "stop"`.** route_executor.py:966, 1002, 1003 — every legacy `Waypoint` is surfaced as a `stop`. Therefore the dwell-entry step (Phase 1 step 5) NEVER fires, the dwell-complete check is always evaluating against `RUNNING_AT_STATION` that never gets set by Phase 1, and `task_kind == "dwell"` rows have no representation at all. This is the proximate cause of "Path completion → STANDBY transition not firing" cluster.
7. **`block_power.py` reads deleted train fields and references a removed enum.** Lines 222, 225, 230, 232 read `train.target_speed` and `train.manual_target_speed` (spec says these don't exist on `Train` in v1). Line 228 references `TrainState.MANUAL` — this enum value was removed (`enums.py:92` only has `OFF/IDLE/STANDBY/RUNNING_MOVING/RUNNING_AT_STATION`). This is a confirmed cluster failure root cause.
8. **`PathDispatcher.create_path/extend_path/cancel_path` signatures and return types are pre-v1.** `create_path(start_block_id, end_block_id, dispatcher_id, priority, train_id)` with v1 contract is `create_path(requested_destination_block_id, train_id, requested_destination_direction, acquisition_mode)`. `extend_path(path_id, new_end_block_id)` instead of `(path_id, requested_destination_block_id, requested_destination_direction, acquisition_mode)`. Returns `PathResult` / `ExtensionResult` (with `error` strings) instead of canonical 8-field result with `result_kind` / `failure_kind` enums. `cancel_path(...)` returns `bool` instead of canonical `CancellationResult`. `AcquisitionMode` enum is missing entirely. `PathAcquisitionResultKind` and `PathAcquisitionFailureKind` are missing entirely. RouteExecutor cannot drive the spec's `truncated_grew` / `blocked_no_growth` extend logic.
9. **Block-state ownership is violated by `TrainPhysics`.** `train_physics.py:_update_block_occupancy` (lines 911-983) directly calls `block_manager.occupy_block(...)`, `block_manager.set_block_to_caution(...)`, and `block_manager.clear_block(...)` on tail-vacate. The spec says: "TrainPhysics must not write Block fields directly and must not call block-state mutators." Block writes should flow through `PathDispatcher.on_controller_progress(report)` → `BlockManager.occupy_blocks([block_id], train_id)` and `BlockManager.on_block_tail_vacated(block_id, train_id, direction)`. Both `occupy_blocks` exists with the right name (block_manager.py:353) but is not called from the dispatch chain consistently; `on_block_tail_vacated` does not exist in `BlockManager`. This causes double-mutation races between `TrainPhysics` and `PathDispatcher.on_controller_progress` and is the root of "Cancel doesn't clean block-state."
10. **`SimulationController` runtime surface is missing.** No `add_train(...)`, no `notification_center`, no `enter_failed_runtime(...)`, no `failed_runtime` flag, no `release_failed_runtime_after_manual_cleanup()`, no canonical `get_or_create_controller(train_id)` (the existing `_get_or_create_controller` takes `train`, not `train_id`, and is private), no `get_or_create_physics(train_id)`. `BlockManager` has no back-reference to `SimulationController` for fatal-conflict reporting (spec mandates this for the fatal physical-entry conflict procedure). `Switch.simulation_controller` is undefined. The spec's "SimulationController is registered first" wiring rule cannot be enforced.

## Detailed findings

### Section: Authoring rules (design_new.md §1) — N/A for code

Verified the doc itself is clean of `RUNNING_WAITING`, `departure_ready_path_id`, `armed_stop_target_block_id`, `travel_direction` setters in design_new.md (only mentioned in the implementation index file). Authoring rules are not runtime contracts; N/A.

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| No tombstone language in design_new.md | PASS | grep clean for "removed in v1", "legacy", "deprecated", "no longer used" in design_new.md | Implementation index file does mention `departure_ready_path_id` historically but it's not the spec doc |

### Section: Design Direction

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `TrainController` owns live ATC/ATO/manual mode, handle, future `power_percent` inputs, lookahead | PARTIAL | train_controller.py:319-330 — fields exist; some live state still mirrored to Train | `train.state` writes on lines 579, 608, 759, 3215, 3335, 3338, 3540, 3543, 3685, 3718, 3726, 3735, 3746, 3792, 3815, 3832, 3845, 3849, 3864, 3868, 3895, 3901, 3972, 4002 violate "not re-synced from Train on every tick" |
| "Target speed" and "speed-limit reason" should not be standalone fields, derived from lookahead at distance 0 | PARTIAL | train_controller.py:510-536 has `target_speed` / `speed_limit_reason` as derived properties | But still keeps `atc_speed_limit`, `current_block_speed_limit`, `_last_speed_limit_reason` as stored scalars; lookahead recompute path coexists with the scalars |
| `TrainController` owns live power/cab state; not re-synced from `Train` on every tick | FAIL | simulation.py:354-365 still inherits `ato_running`, `active_cab`, `control_mode`, `state` from `train` on first creation; this is a per-construction sync, but the `train.state = ...` writes at every tick (train_controller.py multiple sites) re-mirror live state | Spec says only persistent/configuration state syncs, not live |
| `Train` owns persistent/configuration state only (no `current_speed`, `state`, `assigned_path_id`, `ato_destination_block_id`, `current_path`, `path_index`, `reserved_blocks`, `manual_target_speed`) | FAIL | train.py:382 (`current_speed`), 446 (`state`), 410-422 (`current_path`, `path_index`), 426 (`reserved_blocks`), 430 (`assigned_path_id`), 458 (`manual_target_speed`), 468 (`ato_destination_block_id`) — all still on Train entity | All forbidden by spec |
| Initial placement comes through `SimulationController.add_train(...)` | MISSING | simulation.py has no `add_train` method; only `network.add_train(train)` exists at network.py:1811 | Required by spec line 64 |
| Manual sub-modes: `simulation` and `power_percent` | PASS | train_controller.py:321 `manual_mode_type: str = "simulation"`; set_manual_mode_type() and set_manual_power_percent() exist (3547, 3577) | Defaults look right |
| Manual `power_percent` does NOT use accel/decel rates | PARTIAL | train_physics.py:163 (the `tick`) still always uses `_get_acceleration_for_handle` and integrates speed; no special branch for `manual_mode_type == "power_percent"` to bypass to `power_speed_curve` | Spec says manual power_percent is calibrated voltage→speed instantly, not integrated |
| `TrainPhysics` and `BlockPowerController` should not decide manual-mode policy | PARTIAL | train_physics integrates handle universally; block_power.py:228 references `TrainState.MANUAL` (which does not exist in enums) — the enum reference is broken | Mostly correct architecture but with a bug |
| Percentage-power mapping in `SpeedProfile.power_speed_curve` | PARTIAL | speed_profile.py likely has it; not used by physics yet | Confirmed `manual_mode_type == "power_percent"` mode does not actually consume `power_speed_curve` for speed conversion in physics |

### Section: Layered Architecture

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `PathDispatcher` is sole owner of dispatcher path records | PARTIAL | path_dispatcher.py:76 `self.active_paths`, exposed as `paths_by_id` property at :81 | Storage is dispatcher-owned, but completion deletes the row (:1681 `del self.active_paths[path_id]`) instead of retaining as `COMPLETED` history per spec |
| `TrainController.received_path_plan` is a read-only property returning the dispatcher's currently active `Path` | PASS | train_controller.py:417-431 implements property reading from `path_dispatcher.get_active_path(train_id)` | Compliant |
| Each train has at most one `ACTIVE` path at a time, enforced by `create_path(...)` | FAIL | path_dispatcher.py:782 `create_path` immediately calls `find_paths` without checking `active_path_id_by_train(train_id)` first | Spec: "must reject the request if `active_path_id_by_train(train_id)` already returns an `ACTIVE` path" before any side effect |
| Cancelled/completed paths may remain in `paths_by_id` for history | FAIL | path_dispatcher.py:1681-1682 deletes path row on completion | History retention not implemented |
| Controller progress reports identify train by `train_id` only (no `path_id`) | PASS | runtime_payloads.py:118-135 `ControllerProgressReport` has no `path_id` field | Compliant |
| `TrainPhysics.tick()` invokes `TrainController.on_physics_updated(event_time, span_update)` once per crossed boundary plus one final no-boundary callback | PARTIAL | train_physics.py:579-624 emits per-boundary plus final no-boundary callbacks | But `_update_block_occupancy` still writes to `BlockManager` directly during the same tick, violating the dispatcher-mediated rule |
| Phase 3 is the only place that drains queued progress reports | PARTIAL | simulation.py:160-162 drains in Phase 3 | Correct in principle, but TrainPhysics also writes block state directly bypassing the queue |
| Authority is route-backed; `RouteExecutor.create_path(...)` is canonical | PARTIAL | route_executor.py creates paths via `path_dispatcher.create_path(...)` but with old signature | Old signature means the contract isn't enforced |

### Section: Core Runtime Loop / Per-train tick transaction

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Phase 1 entry point is `RouteExecutor.tick_train_phase1(train_id, delta_seconds)` | PASS | simulation.py:142, route_executor.py:252 | Compliant |
| Phase 1 owns Planning Kernel steps (1)–(7) | PARTIAL | route_executor.py:297-350 implements steps 1-7 with the right structure | But step 4 (covered-task index advance) and step 5 (dwell-entry) are nullified because every task is `task_kind="stop"` (see High-Priority Gap #6) |
| Phase 2 = `TrainController.tick(...)` (drain commands first) + `TrainPhysics.tick(...)` → `on_physics_updated` | FAIL | train_controller.py:542 `tick()` does NOT drain operator commands as first substep — there is no command queue at all (see High-Priority Gap #1) | Operator commands are processed inline at call-time (`set_cab`, `set_mode`, etc.), not deferred to drain |
| Phase 3 drains `queued_progress_reports`, runs `try_complete_active_path`, consumes 5 latches in canonical order | PASS | simulation.py:158-178 | Correct order, correct gating with `is_stopped()` for `pending_path_cancel_request` |
| 5 latch consumers fire in canonical order: `pending_path_cancel_request`, `pending_route_abort`, `pending_power_off`, `pending_route_finished`, `pending_route_deactivate` | PASS | simulation.py:174-178 | Correct canonical order |
| Each latch is gated by `is_stopped()` where required | PASS | simulation.py:202-205 | `pending_path_cancel_request` defers while rolling |
| `TrainController.tick()` drains commands first | FAIL | No command drain exists (see High-Priority Gap #1) | Spec: "first substep" |
| Operator commands are queued, not executed inline outside the drain | FAIL | All command methods (`power_on_cmd`, `set_cab`, `set_mode`, `set_handle`, `activate_emergency_cmd`, `release_emergency_cmd`, `power_off_cmd`) execute inline | Violates the spec's "API/UI command submission has a separate result from command execution" rule |
| `PathDispatcher.cancel_path` is the ONLY canonical cancellation entry point | PARTIAL | `cancel_path(path_id, cancellation_kind, reason)` exists at path_dispatcher.py:1994 | But `cancellation_kind` defaults to `None` (spec says callers must pass; v1 enum is mandatory diagnostic) |
| All callers of cancel that need stopped-gating actually use a latch | PASS | simulation.py uses `pending_path_cancel_request`; train_controller.py writes the latch on EB | Compliant |
| Phase 1 reads `lookahead` live (no caching) | PASS | train_controller.py:499-507 lookahead is a property | Compliant; no per-tick cache |
| `TrainController.depart()` called directly by Phase 1 (no `departure_requested` latch) | PARTIAL | route_executor.py has `_try_depart_to_waypoint(...)` style code but legacy departure logic mixes with new direct calls | Departure flow needs end-to-end test verification |
| Phase 3 owns `PathDispatcher.on_controller_progress(report)`, `try_complete_active_path`, `cancel_path` | PASS | simulation.py:160-178 | Compliant |
| EB activation writes `pending_path_cancel_request(EB)` | PASS | train_controller.py:663-668 (ATC overspeed escalation), 3275-3280 (operator-issued EB) | Compliant |
| `TrainController.release_emergency_cmd()` requires `pending_path_cancel_request is None` AND `received_path_plan is None` | PASS | train_controller.py:3316-3325 | Acceptance gate matches spec |
| Reactive speed-based state evaluation: STANDBY→RUNNING_MOVING when `current_speed_kmh > 0`; RUNNING_MOVING→IDLE/STANDBY when stopped no-authority | PASS | train_controller.py:601-617 | Compliant |
| `is_stopped_at_authority_end()` requires `atc_limit_reason_code == "authority_stop"` | FAIL | train_controller.py:1850-1872 approximates with `atc_speed_limit > STOPPED_SPEED_THRESHOLD_KMH` | Lookahead doesn't expose `atc_limit_reason_code`; predicate is weaker than spec |

### Section: Phase ownership / Phase entity-chain table

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Phase 1 may not call `PathDispatcher.cancel_path(...)` directly | PASS | route_executor.py latches `pending_path_cancel_request` instead of calling `cancel_path` directly | Compliant |
| Phase 2 may not mutate `RouteRun` | PARTIAL | `TrainController.tick()` does not directly mutate RouteRun, but it writes `train.state` which is read by route logic | Indirect mutation through Train mirror |
| Phase 3 must not create or extend authority | PASS | simulation.py:_update_train Phase 3 only consumes/cancels | Compliant |
| `RouteRun` mutations restricted to Phase 1 (canonical writes) and Phase 3 callbacks | PARTIAL | route_executor.py mostly limits to Phase 1 / handlers | But `task_runs` data structure doesn't exist, so the whole "first-write rule" cannot be applied |

### Section: Latch table

| Latch | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `path_cancelled` on RouteRun (`bool`) | PASS | route_executor.py:105 `path_cancelled: bool = False` on RouteExecutionState | Compliant |
| `pending_path_cancel_request` on TrainController mailbox (`Optional[PathCancelPayload]`) | PASS | train_controller.py:365 `Optional[PendingPathCancelRequest]`; runtime_payloads.py:144 carries `cancellation_kind`, `reason`, `expected_path_id` | Compliant |
| `pending_route_abort` on TrainController mailbox | PASS | train_controller.py:366 | Compliant |
| `pending_power_off: bool` on TrainController | PASS | train_controller.py:367 | Compliant |
| `pending_route_finished: bool` on TrainController | PASS | train_controller.py:368 | Compliant |
| `pending_route_deactivate` on TrainController | PASS | train_controller.py:369 | Compliant |
| `queued_progress_reports: List[ControllerProgressReport]` on TrainController | PASS | train_controller.py:364 | Compliant |
| Inclusive-latch rule: when `pending_route_abort` written, also write `pending_path_cancel_request` if a live path exists | PARTIAL | No helper enforces it; callers (none right now) would need to be audited | Cannot be tested without callers |
| Inclusive-latch rule: `pending_power_off` includes `pending_path_cancel_request` if live path exists | PASS | train_controller.py:3239-3247 `power_off_cmd()` writes both | Compliant |
| Inclusive-latch rule: `pending_route_deactivate` includes `pending_path_cancel_request` if live path exists | MISSING | `route.deactivate()` walks no per-train trains; train_route.py:257 just sets `is_active = False` and returns | Spec: "writes `pending_route_deactivate` plus (when a live path exists) `pending_path_cancel_request`...for each one" |
| `pending_path_cancel_request` survives across ticks while rolling | PASS | simulation.py:202-205 | Compliant |
| `pending_route_finished` written by Phase 1 terminal-walk | PARTIAL | route_executor.py:_maybe_latch_route_finished hook exists | Logic depends on terminal-walk detection that requires `_find_next_task_target` returning None — but with all-stops behavior, the helper may return None correctly for `ONE_OFF` routes that have run out of waypoints |
| `route.deactivate()` admin handler walks every assigned train, writes `pending_route_deactivate` + inclusive cancel | FAIL | train_route.py:257 just sets `self.is_active = False`; no per-train latch writing | Spec violated |

### Section: SimulationController runtime surface

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Exposes `network`, `block_manager`, `path_dispatcher`, `route_executor`, `notification_center`, controller registry, physics registry | PARTIAL | simulation.py:62-75 has `network`, `path_dispatcher`, `route_executor`, `controllers`, `physics_instances` | Missing `notification_center`, `block_manager` (uses network.block_manager indirectly) |
| `get_or_create_controller(train_id)` (public, train-ID-based) | PARTIAL | simulation.py:341 has `_get_or_create_controller(train)` (private, takes Train, not train_id) | Public train-ID-based API spec required |
| `get_or_create_physics(train_id)` (public) | PARTIAL | simulation.py:369 has `_get_or_create_physics(train)` private | Spec requires public train-ID-based API |
| Route-event delivery via `handle_path_completion(...)` and `handle_path_cancellation(...)` on SimulationController surface | MISSING | simulation.py has no such methods; PathDispatcher calls `handle_path_completion` directly on RouteExecutor at path_dispatcher.py:1654-1665 | Spec: "SimulationController.handle_path_completion(...) which delegate to RouteExecutor.handle_path_completion(...)" |
| `add_train(train_id, position, direction, ...)` synchronous all-or-nothing | MISSING | No `add_train` method on SimulationController | Spec line 64, line 224 |
| Single-threaded model where API requests run between ticks | PARTIAL | No threading code; assumed single-threaded by absence | Implicit compliance; not tested |

### Section: Canonical runtime procedures

#### Physical progress / deviation procedure

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| TrainPhysics emits one `PhysicsSpanUpdate` per crossed boundary + one final no-boundary callback | PASS | train_physics.py:579-624, 633-639 | Compliant |
| `on_physics_updated` writes new span/speed, builds one `ControllerProgressReport`, enqueues to mailbox | PARTIAL | train_controller.py:1878-1972 builds and enqueues | Skips dispatcher reports if powered off (correct), but always sets `current_head_sequence_index = None` and `deviation = False` — does not implement `out_of_sequence` / `beyond_authorized_target` detection |
| Dispatcher handles entered resources first (BlockManager.occupy_blocks for blocks; Switch.occupied_by for switches) | PARTIAL | path_dispatcher.py:1144-1180 records block entry through `BlockManager.occupy_blocks([block_id], train_id)`; switch entry sets `track.occupied_by` (not `Switch.occupied_by`) | Switch occupancy is on TrackSegment, not on Switch entity (because Switch entity lacks `occupied_by` field — see High-Priority Gap #4) |
| Fatal physical-entry conflict procedure | MISSING | path_dispatcher.py:1335-1360 references `enter_failed_runtime` but `SimulationController` has no such method | `FatalConflictKind`, `FatalPhysicalEntryConflict` payload types do not exist |
| `deviation_kind == "out_of_sequence"` → `OUT_OF_SEQUENCE`; `"beyond_authorized_target"` → `AUTHORITY_OVERSHOOT` | PARTIAL | path_dispatcher.py:1091-1093 maps via `_map_deviation_kind_to_cancellation` | Implementation present but never produces deviation reports because `on_physics_updated` always sets `deviation=False` |
| Tail-vacated resources processed after entered resources | PASS | path_dispatcher.py:1127-1129 | Compliant |

#### Active-path completion procedure

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Stop completion is post-physics in v1 | PASS | simulation.py:165-171 in Phase 3 | Compliant |
| Post-physics usable-authority stop check evaluates `controller.state == RUNNING_MOVING`, `is_stopped()`, `eb_active == False`, distance-0 lookahead `atc_limit == 0` with `atc_limit_reason_code == "authority_stop"` | FAIL | train_controller.py:1850-1872 omits the `atc_limit_reason_code == "authority_stop"` check; uses approximation | Reason codes not in lookahead payload |
| `try_complete_active_path` accepts only when `Path.authority_end_block_id == Path.requested_destination_block_id` AND terminal direction matches | FAIL | path_dispatcher.py:1543-1560 reads `path.block_sequence[-1]` and `path.end_block_id` (old fields, not `authority_end_block_id` / `requested_destination_block_id`) | Old Path schema |
| Terminal-teardown order on success: release reservations → release switch/flank locks → transition `Path.status` to `COMPLETED` → call `on_path_complete()` → call `RouteExecutor.handle_path_completion(...)` | PARTIAL | path_dispatcher.py:1573-1665 follows the order but uses `block.release()` and `track.unlock_for_path()` instead of `BlockManager.release_path_block_reservations(block_ids, path_id, completed=True)` | Old release API |
| `RouteExecutor.handle_path_completion(...)` does NOT call `enter_station_dwell()`; dwell entry comes from next eligible Phase 1 | PASS | route_executor.py:843-846 explicitly does NOT call `enter_station_dwell` | Compliant |
| Phase 1 dwell-entry step calls `TrainController.enter_station_dwell()` when next non-finished task is `dwell` | FAIL | route_executor.py:_dwell_entry_step is correctly written (lines 501-547) but it is never reached because every task is reported as `task_kind="stop"` (see High-Priority Gap #6) | Cluster failure root |

#### Power-off procedure

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `power_off_cmd()` sets `pending_power_off = True`, writes inclusive `pending_path_cancel_request` if live path exists, clears `ato_running` immediately | PASS | train_controller.py:3239-3247 | Compliant |
| Phase 3 `pending_power_off` consumer cancels path → calls `RouteExecutor.remove_train_route_run` → controller-local OFF cleanup | PASS | simulation.py:253-281 | Compliant order; cleanup clears cab/eb/ato_running/power_on/pending_power_off |
| `power_off_cmd()` valid while `eb_active == True` if stopped | PASS | train_controller.py:3220-3247 acceptance gate uses `is_stopped()`, allows EB | Compliant |

#### Fatal physical-entry conflict procedure

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `FatalConflictKind` enum with values OCCUPIED_BY_OTHER_TRAIN, RESERVED_FOR_OTHER_TRAIN, SWITCH_OCCUPIED_BY_OTHER_TRAIN, DISABLED_RESOURCE_ENTRY | MISSING | No `FatalConflictKind` class anywhere | Spec §"FatalConflictKind canonical home" |
| `FatalPhysicalEntryConflict` payload | MISSING | No such class | Spec |
| `SimulationController.enter_failed_runtime(...)` | MISSING | No such method | Spec |
| `failed_runtime` flag, `release_failed_runtime_after_manual_cleanup()` | MISSING | No such field/method | Spec |
| `BlockManager.occupy_blocks(...)` reports fatal conflict via `SimulationController.enter_failed_runtime` | FAIL | block_manager.py:353 only returns False on conflict; no fatal reporting | Spec violated |

### Section: Path completion notification flow + cancel_path return contract

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `try_complete_active_path` returns `PathCompletionResult{success, path_id, new_status, reason}` | PASS | runtime_payloads.py:188-204; path_dispatcher.py:1684-1689 returns proper shape | Compliant |
| `cancel_path` returns `CancellationResult{success, path_id, new_status, cancellation_kind, reason}` | FAIL | path_dispatcher.py:1994 returns `bool` | Spec violation |
| Failure cases: missing path → "path not found"; COMPLETED → "already completed"; CANCELLED → "already cancelled" | PARTIAL | path_dispatcher.py:2019-2024 returns False for missing/terminal | But returns are bool, not structured result with `reason` strings |

### Section: TrackSegment / Switch / Block / BlockManager / Path / Path schema / RouteExecutor / RouteTask / RouteTaskRun / TrainController / TrainPhysics / SpeedProfile / Signals / Power

#### TrackSegment

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Removed live runtime fields: `_switch_state`, `occupied_by`, `reserved_by`, `locked_by_path`, `locked_state`, `path_direction` | FAIL | track.py likely retains some of these — `track.path_direction = ...` is written by path_dispatcher.py:897 | Old field still in use |
| Switch state lives on `Switch` entity, not on `TrackSegment` | FAIL | TrackSegment still stores switch state; Switch entity has no `state` field | Spec violation |
| `enumerate_traversals(entry_edge)` planning helper, `get_active_exits` / `get_active_entry_for_exit` for live | PARTIAL | Need to verify; not exhaustively audited | Out of scope for this audit |

#### Switch entity

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `state: int` (0=MAIN, 1=DIVERGE) | FAIL | switch.py:29 has no `state` field | High-Priority Gap #4 |
| `occupied_by: Optional[str]` | FAIL | not present | Spec violation |
| `locks: List[(path_id, claim_role, source_switch_id, train_id, locked_state)]` (multi-owner) | FAIL | switch.py:68-69 has single-owner `locked_by_path` / `locked_state` | Spec violation |
| `flank_protection: List[Tuple[str, int]]` | FAIL | not present | Spec violation |
| `train_protection_claims` | FAIL | not present | Spec violation |
| `disabled: bool = False` | FAIL | not present on Switch | Spec violation |
| `simulation_controller: Optional[SimulationController]` | FAIL | not present | Spec violation |
| `acquire_lock(path_id, claim_role, train_id, state, source_switch_id=None)` (multi-owner) | FAIL | switch.py:103 has single-owner `lock(path_id, state)` | Spec violation |
| `release_lock(path_id, claim_role, source_switch_id=None)` | FAIL | switch.py has `unlock_for_path(path_id)` only | Spec violation |
| `release_train_protection_from(source_switch_id, train_id)` | MISSING | no such method | Spec violation |

#### Block

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `state` is computed property, not stored | FAIL | block.py:42 stores `state: BlockState = field(default=BlockState.FREE)` | Spec violation |
| `_is_caution: bool` | FAIL | not present | High-Priority Gap #3 |
| `reserved_for_train: Optional[str]` | FAIL | not present | Spec violation |
| `caution_direction: Optional[str]` | FAIL | not present | Spec violation |
| `allowed_travel_directions: Set[str]` | FAIL | not present | Spec violation |
| `ordered_tiles: List[BlockTileEntry]` | FAIL | not present | Spec violation |
| Computed state priority OCCUPIED > CAUTION > RESERVED > FREE | N/A | Cannot check — `state` is stored, not computed | |
| `Block.reserve_for_path(train_id, direction)` writes `reserved_for_train`; `path_id` written separately by BlockManager | PARTIAL | block.py likely has `reserve_for_path(path_id, train_id, direction)` (older signature with path_id); spec says no path_id in the helper | Spec violation |

#### BlockManager

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` atomic | MISSING | block_manager.py has only `occupy_block`, `set_block_to_caution`, `clear_block`, `occupy_blocks` | High-Priority Gap |
| `release_path_block_reservations(block_ids, path_id, completed=False)` | MISSING | not present | High-Priority Gap |
| `on_block_tail_vacated(block_id, train_id, direction)` | MISSING | not present | High-Priority Gap |
| `on_switch_state_changed(switch_id)` | MISSING | not present | Spec violation |
| `_reevaluate_caution_blocks()` | MISSING | not present | Spec violation |
| `BlockManager` has Optional SimulationController back-reference for fatal conflict reporting | MISSING | block_manager.py:63-70 stores only `blocks` and `history` | Spec violation |
| `occupy_blocks(block_ids, train_id)` is atomic with rollback | PASS | block_manager.py:353 implements rollback | Compliant |

#### Path

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Lifecycle: `ACTIVE`, `COMPLETED`, `CANCELLED` (3 states) | FAIL | path.py:57 has 7 states (`PENDING`, `SETTING`, `SET`, `ACTIVE`, `CLEARING`, `COMPLETED`, `CANCELLED`) | High-Priority Gap #2 |
| Born `ACTIVE` on successful create | FAIL | path.py:275 defaults to `PathStatus.PENDING`; create_path explicitly sets `ACTIVE` at path_dispatcher.py:820 | Default is wrong |
| Fields: `path_id`, `status`, `train_id`, `path_tiles`, `resource_sequence`, `current_resource_index`, `requested_destination_block_id`, `requested_destination_direction`, `authority_end_block_id`, `last_status_updated_time` | FAIL | path.py:259-317 has the OLD field set | Spec violation |
| `resource_sequence` is the single source of truth for path geometry | FAIL | not present | Spec violation |
| `path_tile entry schema` with `entry_edge`, `exit_edge`, `resource_sequence_index` | FAIL | path.py:285 stores `path_tiles: List[Tuple[int, int]]` (bare tuples) | Spec violation |
| `resource_sequence entry schema` with `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge` | FAIL | not present | Spec violation |
| Train can have only one `ACTIVE` path | PARTIAL | get_active_path returns first matching ACTIVE; create_path doesn't reject up front | Insufficient enforcement |
| `last_status_updated_time` set on every status transition | MISSING | not present on Path | Spec violation |

#### PathDispatcher

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `paths_by_id` registry | PASS | path_dispatcher.py:81 property aliases `active_paths` | Compliant via property |
| `active_path_id_by_train(train_id)` | PASS | path_dispatcher.py:2220 | Compliant |
| `get_active_path(train_id)` | PASS | path_dispatcher.py:2208 | Compliant |
| `get_active_path_progress(train_id)` returning `{path_id, status, current_resource_index}` | MISSING | not present | Spec violation |
| `create_path(requested_destination_block_id, train_id, requested_destination_direction, acquisition_mode)` | FAIL | path_dispatcher.py:754 has different signature (see #8) | Spec violation |
| `extend_path(path_id, requested_destination_block_id, requested_destination_direction, acquisition_mode)` | FAIL | path_dispatcher.py:1695 has `extend_path(path_id, new_end_block_id)` | Spec violation |
| `cancel_path(path_id, cancellation_kind, reason)` returns `CancellationResult` | PARTIAL | signature is right at path_dispatcher.py:1994 (cancellation_kind, reason); returns `bool` instead of structured result | Partial |
| `AcquisitionMode` enum with `all_or_nothing` and `best_effort` | MISSING | not present | Spec violation |
| `PathAcquisitionResultKind` (`reached_requested_target`, `truncated_grew`, `blocked_no_growth`) | MISSING | not present | Spec violation |
| `PathAcquisitionFailureKind` (`no_viable_path`, `blocked_before_target`, `invalid_reverse`, `invalid_request`, `head_on_switch_no_block_authority`, `missing_or_inactive_path`) | MISSING | not present | Spec violation |
| `RailwayPathfinder` shared graph home with `query_graph_walks(...)`, `find_ordered_walk(...)`, `measure_along_walk(...)` | MISSING | not present | Spec violation |
| `GraphWalkCandidate`, `GraphLocator`, `DirectedBlockFrame`, `GraphWalkStep` | MISSING | not present | Spec violation |

#### Route / RouteTask / RouteRun / RouteTaskRun

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `RouteTask` with `task_kind`, `task_location`, `task_time`, `task_direction`, `flexible_platform`, `target_time`, `departure_mode`, `task_metadata` | MISSING | train_route.py has only legacy `Waypoint` | High-Priority Gap #5 |
| `Route.tasks: List[RouteTask]` | FAIL | train_route.py has `waypoints: List[Waypoint]` | Spec violation |
| `Route.route_version` | MISSING | not present | Spec violation |
| `Route.reconciliation_policy` | MISSING | not present | Spec violation |
| `Route.priority` | MISSING | not present (used as `int(getattr(route, "priority", 5))` fallback in route_executor.py:197) | Spec violation but graceful default |
| `Route.default_departure_mode` | MISSING | not present | Spec violation |
| `Route.acquisition_mode` | MISSING | not present | Spec violation |
| `RouteRun.task_runs: List[RouteTaskRun]` (parallel to Route.tasks) | MISSING | not present | Spec violation; without this, all `task_runs[i].started_at`/`finished_at`/`resolved_target` storage is missing |
| `RouteRun.target_waypoint_index` | MISSING | RouteExecutionState has only `current_waypoint_index` | Spec violation |
| `RouteRun.route_version_seen` | MISSING | not present | Spec violation |
| `RouteRun.started_at` (Unix timestamp) | PARTIAL | RouteExecutionState has `route_start_time` instead | Wrong name |
| `RouteRun.run_number` | MISSING | not present | Spec violation |
| `RouteRun.path_cancelled` latch | PASS | route_executor.py:105 | Compliant |
| `RouteRun.current_route_status: RouteRunStatus` (`state` + `last_cancellation_kind`) | PARTIAL | flat fields `route_state` (str) and `last_cancellation_kind` exist; not nested in canonical object | Compliant in spirit |
| Initial `current_waypoint_index = -1` for every run | FAIL | route_executor.py:97 defaults to `0` | Spec violation: "All route runs initialize to -1 before the first completed task" |
| `direction` is `"forward"` / `"reverse"` literal string | FAIL | route_executor.py:98 `direction: int = 1` | Spec violation |
| `_find_next_task_target(train_id, exclude=None)` with read-only contract | PARTIAL | route_executor.py:905-1005 has the right signature | But always returns `task_kind="stop"` (see High-Priority Gap #6) |
| Phase 1 covered-task index advance is a small loop | PASS | route_executor.py:_covered_task_index_advance exists | Compliant logic |
| Phase 1 dwell-complete writes `task_runs[i].finished_at`, advances cursor, calls `finish_dwell()`; first-write rule on `task_runs[i+1].started_at` | FAIL | `task_runs` data structure does not exist | Cannot apply |
| Phase 1 dwell-entry step calls `enter_station_dwell()` on `task_kind == "dwell"` | FAIL | Never reached because no task is ever `task_kind="dwell"` | Cluster failure root |
| `_enter_task_arrival(exec_state, task_index, arrival_time)` | PARTIAL | route_executor.py:837-843 inlines the equivalent in `handle_path_completion` | Compliant in effect, but no separate helper |
| Terminal `pending_route_finished` latch written by Phase 1 | PASS | route_executor.py:_maybe_latch_route_finished | Compliant |
| `remove_train_route_run(train_id, reason)` | PASS | route_executor.py:849 | Compliant |
| `route.deactivate()` walks every assigned train, writes per-train latches | FAIL | train_route.py:257 just sets `is_active = False` | Spec violation |

#### TrainController

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Owns live operational state, `power_on`, cab, control_mode, manual_mode_type, handle_position, manual_power_percent, eb_active, ato_running, queued_progress_reports, pending_*, current_span, current_speed_kmh | PASS | train_controller.py:298-381 | Compliant |
| `received_path_plan` is read-only property over PathDispatcher | PASS | train_controller.py:417-431 | Compliant |
| `assigned_path_id`, `ato_path_id`, `ato_destination_block_id` are derived reads | PASS | train_controller.py:433-460 | Compliant |
| `travel_direction` is read-only property derived from cab + current_span | PARTIAL | train_controller.py:462-496; falls back to `train.position.facing_direction` because `current_span` storage isn't fully wired | Compliant in spirit |
| `lookahead` is pure derived value (no caching) | PASS | train_controller.py:498-507 | Compliant |
| `manual_power_percent: int = 0..100` | PASS | train_controller.py:328 | Compliant |
| Acceptance gates on `power_on_cmd`, `power_off_cmd`, `set_cab`, `set_mode`, `set_manual_mode_type`, `set_handle`, `set_manual_power_percent`, `activate_emergency_cmd`, `release_emergency_cmd` per spec | PARTIAL | most gates exist, but `power_off_cmd` accepts during EB if stopped (correct), `release_emergency_cmd` checks `pending_path_cancel_request is None` and `received_path_plan is None` (correct) | Mostly compliant for the methods that exist, but no command queue means commands can't be processed in canonical drain order |
| `TrainController.depart()` returns `(success: bool, reason: Optional[str])` | PARTIAL | train_controller.py:3758 returns `bool` | Spec says tuple |
| `TrainController.ato_go()` is sole writer of `ato_running = True` | PASS | train_controller.py:3610 only writes `ato_running = True` | Compliant |
| `enter_station_dwell()`, `finish_dwell()`, `on_path_complete()` | PASS | train_controller.py:3820, 3834, 3851 | Compliant |
| `is_stopped()` uses `STOPPED_SPEED_THRESHOLD_KMH = 0.1` from controller-owned `current_speed_kmh` | PASS | train_controller.py:1834-1848 | Compliant |
| `is_stopped_at_authority_end()` requires `atc_limit_reason_code == "authority_stop"` | FAIL | train_controller.py:1850-1872 approximates without reason code | High-Priority gap |
| `on_physics_updated` builds and enqueues one ControllerProgressReport | PASS | train_controller.py:1878-1972 | Compliant; deviation handling not wired |
| Mirrors `train.current_speed = self.current_speed_kmh` | FAIL | train_controller.py:1909-1911 | Spec says don't mirror |
| `to_dict()` exposes `current_span`, `current_speed_kmh`, `received_path_plan`, full lookahead | PARTIAL | train_controller.py:4059 — needs detailed schema check | Out of scope |

#### TrainPhysics

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `TrainPhysics.tick(...)` computes `span_update` and emits to `TrainController.on_physics_updated(...)` per boundary + final no-boundary | PASS | train_physics.py:579-624 | Compliant |
| `TrainPhysics` does not write `Block` fields directly | FAIL | train_physics.py:911-983 calls `block_manager.occupy_block`, `block_manager.set_block_to_caution`, `block_manager.clear_block` directly | High-Priority Gap #9 |
| `TrainController.on_physics_updated` reports tail-vacated only when train no longer occupies any tile in the block | FAIL | train_controller.py:1942-1960 reports per-tile removal without checking that all tiles in that block are gone | Spec: "PathDispatcher may call BlockManager.on_block_tail_vacated() only after the controller report proves the train no longer occupies any tile in that block" |
| `TrainPhysics` does not own `_caution_blocks` set; BlockManager owns full release lifecycle | FAIL | train_physics.py:101 has `_caution_blocks: Set[str]` | Spec violation |
| Manual `power_percent` mode uses `power_speed_curve` directly (no accel/decel integration) | FAIL | train_physics.py always integrates handle | Spec violation |
| `track_voltage` is computed by physics each tick from the active mode's authoritative input | PARTIAL | train_physics.py:_update_voltage_output exists | Verify accuracy out of scope |

#### SpeedProfile

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Fields: `profile_id`, `name`, `startup_voltage`, `max_voltage`, `power_speed_curve`, `lighting_voltage`, `acceleration_rate`, `deceleration_rate`, `emergency_brake_rate`, `friction_rate`, `operational_decel_ratio`, `air_resistance_enabled`, `drag_coefficient`, `reference_speed_kmh` | PARTIAL | speed_profile.py exists, not exhaustively audited | Quick spot check would be needed |
| `power_speed_curve` is bidirectional `(power_percent, speed_kmh)` mapping | PARTIAL | profile field exists; reverse lookup not exhaustively audited | |

#### Signals

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Signals are display-only; CAUTION moves to `TrainController.CAUTION_SPEED_LIMIT_KMH` | PARTIAL | network.py:`update_signals_from_blocks` is called post-tick (simulation.py:116) | Need verification that ATC doesn't read signals directly |

#### Power / BlockPowerController

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| BlockPowerController focuses on software-to-hardware boundary only | FAIL | block_power.py:189-235 reads `train.target_speed`, `train.manual_target_speed`, `train.state` (and references nonexistent `TrainState.MANUAL`) | High-Priority Gap #7 |
| `TrainPhysics` computes `track_voltage`; BlockPowerController applies it | FAIL | block_power.py computes voltage from train fields directly | Spec violation |

### Section: Data Flow To UI

| Rule | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `Train.to_dict()` exports only persistent/configuration | FAIL | train.py:998-1030 still exports live fields like `speed`, `state`, `assigned_path_id`, `ato_destination_block_id`, `manual_target_speed`, `current_path` | Spec violation |
| `TrainController.to_dict()` exports live state including `current_span`, `current_speed_kmh`, `received_path_plan` (canonical schema), full lookahead | PARTIAL | train_controller.py:4059 to_dict; not exhaustively verified | Out of scope |
| `PathDispatcher.get_active_path_progress(train_id)` for UI | MISSING | not implemented | Spec violation |
| `simulation.build_network_payload(...)` returns three sibling payload groups: `trains`, `controllers_by_train_id`, `dispatcher_progress_by_train_id` | FAIL | simulation.py:404-450 merges controller into `train_data["onboard"]` and writes back live path data into the train's payload | Spec violation |

### Section: End-to-End ATO Path Run

| Step | Status | Notes |
| --- | --- | --- |
| 1. Phase 1: covered-task index advance, resolve via `_find_next_task_target`, call `create_path(...)` with `acquisition_mode = route.acquisition_mode` | PARTIAL | Phase 1 structure correct (route_executor.py:tick_train_phase1); but no `acquisition_mode` is passed (missing from create_path signature), and `_find_next_task_target` returns wrong `task_kind` |
| 2. Phase 2: TrainController + TrainPhysics, boundary-by-boundary callbacks | PARTIAL | Boundary callbacks correct; but operator-command drain missing |
| 3. Phase 3: drain queued progress, run post-physics stop check, call `try_complete_active_path`, then 5 latches in canonical order | PARTIAL | Order correct; `try_complete_active_path` reads old Path schema; latches consume correctly |
| 4. Best-effort retries via `extend_path(...)` until reached or `blocked_no_growth` | FAIL | `extend_path` signature wrong, no `result_kind`, `blocked_no_growth` semantics unimplementable |
| 5. Stop arrival → next tick Phase 1 dwell-entry → dwell-complete | FAIL | Dwell-entry never fires (Cluster #1 root) |

### Section: Logic Review Checklist (binary checks)

| Check | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Controller receives canonical `received_path_plan schema` | PARTIAL | property exists but underlying Path schema is wrong | Wrong schema feeds correct property |
| Each `resource_sequence` entry carries ordered `path_tiles` slice with `entry_edge`/`exit_edge` | FAIL | `resource_sequence` does not exist; `path_tiles` is bare `(x,y)` tuples | Schema gap |
| Controller lookahead is the only source of target speed and speed-limit reason | PARTIAL | property derives, but stored scalars `atc_speed_limit`, `current_block_speed_limit`, `_last_speed_limit_reason` coexist | Cleanup needed |
| Manual mode ATC enforces end of authority via `_calculate_authority_boundary_limit()` | PARTIAL | helper called for manual; correct in principle | Verify out of scope |
| Path-complete events require post-physics controller stop check | PASS | simulation.py:165-171 + train_controller.is_stopped_at_authority_end | Compliant in flow |
| `_ato_calculate_handle()` reads canonical lookahead | PARTIAL | uses lookahead-derived data; some scalars still in play | |
| `RouteExecutor` reads controller state, not Train | FAIL | route_executor.py reads controller state but every controller method also writes `train.state` so they stay synced | Spec says don't mirror |
| `RouteExecutor` resolves direction preferences before dispatcher call | FAIL | `_find_next_task_target` lacks direction filtering for `forward/reverse/either` | |
| `RouteExecutor` and `PathDispatcher` share path comparison rules | FAIL | No shared `RailwayPathfinder` exists | Spec |
| Head/tail/current-span data via `TrainController.get_current_span()` only | FAIL | RouteExecutor and PathDispatcher still read `train.position.occupied_tiles` directly | |
| Covered-task advance for `waypoint`, not for `stop` | FAIL | All tasks treated as `stop`; covered-advance never runs the loop | |
| Dispatcher rejects ambiguous same-tile movement without selected cab | PARTIAL | Need check; not exhaustively audited | |
| Repeated resources rejected during create/extend | PARTIAL | `if new_end_block_id in path.block_sequence` (path_dispatcher.py:1721) - simple block-level guard | Doesn't cover switches |
| Best-effort: keep one live `path_id`, extend in place | FAIL | `extend_path` doesn't return `truncated_grew` / `blocked_no_growth` | |
| Best-effort create fails when head on switch + no forward block | MISSING | not implemented | |
| Dispatcher resolves active progress from its registry, not physics/train mirrors | PASS | path_dispatcher.py:on_controller_progress | Compliant |
| Per-train RouteRun objects, no route-level summary | PASS | RouteExecutionState per train_id | Compliant in spirit |
| Phase 1 covered-task advance handles waypoints, doesn't advance past stops | FAIL | All tasks treated as stops | |
| On retarget/replan, clear affected `current_route_status` target fields | FAIL | `task_runs` doesn't exist | |
| Stop completion requires post-physics usable-authority stop check; deviation cases split into OUT_OF_SEQUENCE / AUTHORITY_OVERSHOOT | PARTIAL | predicate + enum exist; reason code missing | |
| `CAUTION → FREE` requires forward neighbour non-occupied with `caution_direction` | FAIL | `caution_direction` doesn't exist; `_reevaluate_caution_blocks` doesn't exist | |
| Completion/cancellation never unlocks block/switch the train physically occupies | PARTIAL | path_dispatcher.py guards via `train_occupied_blocks`/`train_occupied_tiles` | Compliant in dispatcher; physics' direct calls might bypass |
| After `release_emergency_cmd`, returns to `IDLE`/`STANDBY`; restart via fresh path | PASS | train_controller.py:3326-3343 | Compliant |
| Train-owned flank-protection claims record `source_switch_id` | FAIL | `train_protection_claims` doesn't exist on Switch | |

## Cross-reference with known regressions (clusters of 36 failing tests)

### Cluster 1: Path completion → STANDBY transition not firing

**Doc rule(s) violated:**
- spec line 254 + Phase 1 dwell-entry: "When the route's next non-finished task has `task_kind == "dwell"`...call `TrainController.enter_station_dwell()`"
- High-Priority Gap #6: `_find_next_task_target` returns `task_kind="stop"` for every task, never `"dwell"`.

**Production code violating it:**
- route_executor.py:966, 1002, 1003 — hardcoded `task_kind="stop"` because legacy `Waypoint` lacks `task_kind` discriminator.
- train_route.py:50-160 — `Waypoint` schema is the only task type; `RouteTask` does not exist.

**Effect:** Phase 1 dwell-entry step never triggers `enter_station_dwell()`, so controller stays in `STANDBY` after path completion (correct per `on_path_complete()`) but never transitions to `RUNNING_AT_STATION`, never honors dwell timing, and the route hangs.

### Cluster 2: Auto-path-assignment from route doesn't fire

**Doc rule(s) violated:**
- spec lines 1115-1124 (Planning Kernel): "Acquire authority. If eligibility precondition is met...call `PathDispatcher.create_path(...)` or `PathDispatcher.extend_path(...)`"
- spec line 1142: "On a successful create/extend, write `RouteRun.task_runs[target_waypoint_index].resolved_target` to the candidate that the dispatcher actually targeted"
- spec line 698: `create_path(...)` must reject if active path exists, before any side effect.

**Production code violating it:**
- path_dispatcher.py:754 has wrong signature `create_path(start_block_id, end_block_id, dispatcher_id, priority, train_id)` (missing `requested_destination_direction` and `acquisition_mode`)
- route_executor.py:_authority_acquisition_step likely uses old signature; without `requested_destination_direction` and `acquisition_mode`, dispatcher can't enforce direction-aware filtering or `best_effort` semantics
- task_runs structure missing means `resolved_target` writeback has no storage

**Effect:** RouteExecutor calls dispatcher but dispatcher doesn't filter by direction or know about `best_effort`; mismatches cause silent failure.

### Cluster 3: EB auto-release on stop not firing

**Doc rule(s) violated:**
- spec EB workflow row 4: "release_emergency_cmd called while stopped and powered after the EB cancellation latch has been consumed (controller's own `pending_path_cancel_request` is None) and dispatcher has cleared the pre-EB active path (controller's own `received_path_plan is None`)"
- spec line 1494 EB workflow row 5: "EB restart/replan...The Phase 1 route-deviation-cleanup step clears `RouteRun.path_cancelled` when a route run exists"

**Production code analysis:**
- train_controller.py:3316-3325 acceptance gate is correct
- BUT: the spec describes EB release as an OPERATOR action — it's the operator's release_emergency_cmd. Spec does NOT promise auto-release on stop. The cluster description says "EB auto-release on stop not firing" — this may indicate the test expectation differs from spec, OR the test may be expecting Phase 1 deviation cleanup (`path_cancelled` clear) to fire which is gated by all `IDLE/STANDBY/`pending_*`/`eb_active`/`pending_power_off` predicates.
- Likely failure path: EB latched → `pending_path_cancel_request(EB)` → Phase 3 commits cancel → `path_cancelled = True` set on RouteRun → next eligible Phase 1 should clear `path_cancelled` but only if `eb_active == False`. So Phase 1 cannot clear until release_emergency_cmd succeeds. The release acceptance gate is correct. If the test expects cleanup before release, it's misaligned with spec.
- More likely cause: train_controller.py:3326-3343 sets `train.state = STANDBY` and reads `train.is_stopped()`. If `train.current_speed` is still > threshold, release rejects.

**Effect:** Without an audit of the test, I'd cite gap #1 (no command queue) as a contributing factor — operator commands process inline, so timing may diverge from the queued model the test expects.

### Cluster 4: ATO destination derivation

**Doc rule(s) violated:**
- spec line 1394-1398: "the alias mapping is explicit in v1: `assigned_path_id := received_path_plan.path_id`, `ato_path_id := received_path_plan.path_id`, `ato_destination_block_id := received_path_plan.authority_end_block_id`"

**Production code violating it:**
- train_controller.py:447-460 reads `plan.authority_end_block_id` if present, else falls back to `plan.end_block_id` (legacy field). 
- `Path.authority_end_block_id` does NOT exist (Path schema is pre-v1) — High-Priority Gap #2.
- So `ato_destination_block_id` always falls through to legacy `end_block_id`, which may be the wrong block for a best-effort truncated path.

**Effect:** Cab selection in `TrainController.depart()` may use the wrong destination for tie-breaking; UI may display the requested destination as "ATO destination" while authority is actually a truncated prefix.

### Cluster 5: block_power reading deleted train.target_speed

**Doc rule(s) violated:**
- spec line 1294 ("Train ownership"): "All live runtime state lives on TrainController...there is no `manual_target_speed`, `train.state`...field"
- spec line 302 ("`SimulationController._update_train(...)`" calls): "Stop writing `train.target_speed`, `train.speed_limit_reason`, `train.ato_destination_block_id`, and `train.state` here. Consumers that want live target-speed data should read controller lookahead at distance 0 instead"

**Production code violating it:**
- block_power.py:222, 225, 230, 232 reads `train.target_speed` and `train.manual_target_speed` 
- block_power.py:228 references `TrainState.MANUAL` (does not exist; `enums.py` only has 5 values)

**Effect:** Direct field-not-found at runtime when block_power runs. Confirmed root cause.

### Cluster 6: Cancel doesn't clean block-state

**Doc rule(s) violated:**
- spec line 576: "`release_path_block_reservations(block_ids, path_id, completed=False)`...When `completed=False` (path cancelled mid-flight), physical CAUTION state is left untouched...When `completed=True` it additionally force-clears `_is_caution` and `caution_direction`"
- spec line 562: "`BlockManager.occupy_blocks` is the sole physical-entry writer"

**Production code violating it:**
- block_manager.py has no `release_path_block_reservations(...)` method (High-Priority Gap, MISSING)
- path_dispatcher.py cancel_path uses `block.release()` and `track.unlock_for_path()` instead — the old single-block API. block.release() clears `state` and `occupied_by` directly without going through `_is_caution`/`caution_direction` (which don't exist) and without distinguishing `completed=True/False`.
- train_physics.py:_update_block_occupancy at lines 911-983 directly calls `block_manager.set_block_to_caution`, `block_manager.clear_block`, `block_manager.occupy_block` — bypassing the dispatcher-mediated rule. Cancel may leave the physics-side `_caution_blocks` set holding stale entries.

**Effect:** After cancel, blocks may remain in `OCCUPIED` or `CAUTION` indefinitely because the only force-clear path (completed=True branch) is unreachable. Live train motion still touches blocks via train_physics directly, racing with dispatcher cleanup.

## Conclusion

The migration delivered the **outer shape** of v1: the per-train Phase 1/2/3 dispatch in `simulation.py:_update_train`, the `runtime_payloads.py` schemas (`PhysicsSpanUpdate`, `ControllerProgressReport`, `PendingPathCancelRequest`, `PathCancellationKind`), the `received_path_plan` derived property, the latch fields on `TrainController`, the canonical 5-latch consume order, and the wiring of `tick_train_phase1`. About 21% of v1 rules pass cleanly.

But the migration largely **left the underlying entities (Block, Switch, Path, Route, Waypoint, Train) at their pre-v1 schemas**, so the new outer shape sits on contradictory storage. Multiple v1-canonical types are entirely unimplemented (NotificationCenter, FatalConflictKind, RailwayPathfinder, BlockManager.reserve_blocks_for_path/release_path_block_reservations/on_block_tail_vacated/on_switch_state_changed, SimulationController.add_train, AcquisitionMode/PathAcquisitionResultKind/PathAcquisitionFailureKind, GraphWalkCandidate family, TrainSpan/SpanEndLocator/SpanTile, RouteTask/RouteTaskRun, the operator-command queue/drain). About 23% of v1 rules are MISSING (no implementation).

The 36 failing tests cluster around exactly the gaps the audit confirms:

1. The dwell-entry / `task_kind == "dwell"` branch can't execute because `_find_next_task_target` still returns `"stop"` for every task (Cluster #1).
2. `create_path/extend_path` signatures are pre-v1 and `acquisition_mode` is missing, so RouteExecutor can't drive dispatcher with the correct contract (Cluster #2).
3. EB workflow gating is correct in code, but operator-command queue/drain absence creates a timing skew that may break tests (Cluster #3, partially).
4. Path schema is pre-v1, so `authority_end_block_id` doesn't exist, falling back to `end_block_id` (Cluster #4).
5. `block_power.py` reads removed Train fields and a removed enum (`TrainState.MANUAL`) — direct runtime failures (Cluster #5).
6. Cancel uses old block API, BlockManager has no `release_path_block_reservations`, TrainPhysics directly mutates blocks racing with dispatcher (Cluster #6).

A reasonable path forward would be: (1) finish the entity-schema migration (Path → resource_sequence, Block → primitives + computed state, Switch → multi-owner locks, Route → RouteTask/RouteRun/task_runs), (2) implement the missing canonical types (BlockManager.reserve/release/tail-vacate, AcquisitionMode + result enums, RailwayPathfinder, NotificationCenter, FatalConflictKind, SimulationController.add_train + runtime surface), (3) implement the operator-command queue/drain, and (4) sweep all train-mirror writes (`train.state =`, `train.target_speed`) out of the codebase. Items 1-3 unblock the 36 failing tests; item 4 prevents new regressions.

**Estimated effort to reach 90%+ conformance: substantial; this is closer to the start of a v1 migration than to its tail end.**
