> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Glossary

This section defines vocabulary that is overloaded across the document — terms that mean similar but distinct things in different layers, or that appear in prose without a stored field backing them. Defining each term once here lets every other site cross-reference by name rather than restating.

Three discipline rules govern this glossary:

1. **If a word appears in prose but not in any schema, it goes in the glossary or it does not appear in prose.** No informal nouns.
2. **If two words mean the same thing across layers, pick one as canonical and note the other as a synonym.** Do not let two words drift apart over time.
3. **If a word means different things in different contexts, qualify it every time it appears.** Never bare `forward` — always "block-relative forward" or "train-relative forward."

**Group A — Action verbs.** Each row names the entity acted on, who initiates the action, and the resulting state.

| Verb | Entity / initiator / resulting state |
| --- | --- |
| `cancel` | `Path` (`status: ACTIVE -> CANCELLED`); initiated by `PathDispatcher.cancel_path(...)` driven by an explicit Phase-3 latch (`pending_path_cancel_request`) or by movement-derived deviation handled inline in Phase 3 |
| `complete` | `Path` (`status: ACTIVE -> COMPLETED`); initiated by `PathDispatcher.try_complete_active_path(...)` after the post-physics usable-authority stop check passes |
| `finish` | `RouteRun` (`current_route_status.state -> FINISHED`); initiated by `RouteExecutor.finish_route_run(train_id)` when Phase 1 detects the terminal walk |
| `deactivate` | `RouteRun` (`current_route_status.state -> DEACTIVATED`); initiated by `RouteExecutor.remove_train_route_run(...)` from the Phase-3 abort/power-off/deactivate consumers |
| `abort` | `RouteRun`; operator-driven teardown via the `pending_route_abort` latch plus inclusive `pending_path_cancel_request(operator_cancelled)` |
| `revoke` | `Path`; system-driven cancel via `pending_path_cancel_request(revoked)` from route reconciliation or admin route deactivation |
| `deviate` | `RouteRun.path_cancelled = True` (derived `display_state == "DEVIATED"`); set by `RouteExecutor.handle_path_cancellation(...)` after any path cancel |
| `depart` | `TrainController.ops.state -> RUNNING_MOVING`; initiated by `TrainController.depart()` called by `RouteExecutor.tick_train_phase1(...)` when live authority exists |
| `dwell` | `TrainController.ops.state -> RUNNING_AT_STATION`; initiated by `TrainController.enter_station_dwell()` called by `RouteExecutor` Phase-1 dwell-entry step |

**Group B — Geometry terms.** Each row names the frame of reference. Always qualify the term in prose.

| Term | Frame of reference |
| --- | --- |
| `forward` | Block-relative (matches `Block.allowed_travel_directions` and `Block.ordered_tiles` order) OR train-relative (the cab end the train is heading toward). Always qualify which. |
| `reverse` | Block-relative or train-relative. Always qualify which. |
| `head` | Train-relative; the physical end of the current span on the authority side, i.e. closer to `authority_end_block_id`. |
| `tail` | Train-relative; the physical end of the current span farther from `authority_end_block_id`. |
| `cab` | Operator-selected control end of the train; `A` or `B` or `None`. The cab is on the head side when the train is moving forward in the authority frame. |
| `frame` | Generic term for a directed coordinate context; always qualify with one of: block-relative, traversal, terminal, or travel. |
| `traversal direction` | Block-relative; `"forward"` or `"reverse"` for one block as walked by a path. |
| `travel direction` | Network-relative; the train's current motion direction in the network frame, derived from cab and span. |
| `terminal frame` | The directed `path_tiles` / `entry_edge` / `exit_edge` of the terminal block in a `Path.resource_sequence`; used for braking and for completion direction match. |

**Group C — Aggregate nouns.** Each row names the type, or marks the term as informal-prose-only with the canonical concrete artifact.

| Noun | Type / canonical schema |
| --- | --- |
| `path` | Stored entity (`Path` record); canonical schema in **Path field schema**. |
| `authority` | Informal prose for "the cleared corridor ahead of the train"; not a stored field. Use `Path` or `received_path_plan` when the concrete artifact is needed. |
| `plan` | Informal prose synonym for `received_path_plan`; canonical schema in **received_path_plan schema**. |
| `run` | `RouteRun` runtime instance; canonical schema in the RouteRun section. |
| `walk` | The directed task index sequence of a `RouteRun` (the ordered list of `task_runs[i]` indexes); not a stored field. |
| `task` | `RouteTask` authored entry (with `task_kind`); runtime instance is `RouteTaskRun`. |
