> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Data Flow To UI

**API payload ownership**

- Each runtime/entity owner should expose its own API payload through its own `to_dict()` (or equivalent read-model method) rather than merging changed live fields back into another entity's payload. `Train.to_dict()` owns persistent/configuration train data. `TrainController.to_dict()` owns live onboard/control/physical diagnostics. Dispatcher-owned path progress is exposed through `PathDispatcher.get_active_path_progress(train_id)`.
- In v1, any composite runtime snapshot that currently returns the network/train view must expose three sibling payload groups with this exact ownership split: `trains` as the list of `Train.to_dict()` payloads, `controllers_by_train_id` as a map `train_id -> TrainController.to_dict()`, and `dispatcher_progress_by_train_id` as a map `train_id -> {path_id, status, current_resource_index}` resolved from `PathDispatcher.get_active_path_progress(train_id)`. Other detail endpoints may present the same owned data in a different wrapper, but they must preserve this ownership split and must not merge controller-owned or dispatcher-owned live fields back into `Train.to_dict()`.

**`Train.to_dict()` payload**

- `to_dict()` should expose all canonical fields owned by the `Train` entity for convenience.
- Export only persistent/configuration train state from `Train.to_dict()`, because those are the fields owned by `Train`.
- `Train.to_dict()` exports only the persistent/configuration fields owned by `Train`. Live physical/control diagnostics (position, span, speed, state, target speed, ATO info, lookahead, path/destination IDs, reserved blocks) are not in this payload — they live on the controller payload.
- Read live operational diagnostics from `TrainController.to_dict()` instead.

**`TrainController.to_dict()` payload**

- `to_dict()` should expose all canonical fields owned by the `TrainController` entity for convenience.
- Export all live operational state, including `handle_position`, `manual.manual_mode_type`, and `manual.manual_power_percent`.
- Export live physical state from the controller-owned storage, including `current_span` and `current_speed_kmh`, for UI/debug consumers that previously read position or occupied tiles from `Train.to_dict()`.
- Export ATO state using `ato_destination_block_id` derived from `received_path_plan.authority_end_block_id`.
- Export `received_path_plan` when present using the canonical **received_path_plan schema**. This is the controller/UI projection of active dispatcher authority, and its nested authority geometry must continue to follow the canonical **resource_sequence entry schema** and **path_tile entry schema**.
- Do not make `TrainController.to_dict()` the owner of live path-progress state in v1. `PathDispatcher` remains the sole owner of `current_resource_index`; if UI needs that progress projection, expose it through a dispatcher-owned read model such as `PathDispatcher.get_active_path_progress(train_id) -> {path_id, status, current_resource_index}`, resolved from the dispatcher's own active-path registry for that `train_id`, rather than copying ownership into `TrainController` or writing per-tick sequence-index updates into the controller.
- Do not keep `TrainController.path_projection.get_path_tiles()` in v1. UI/debug consumers that need active authority geometry should read `received_path_plan.resource_sequence`; consumers that need current physical occupancy should read `TrainController.get_current_span()`.
- Expose the full canonical lookahead payload as the single source of truth for current target speed, ATC limit, speed-limit reason, and forward curve rendering.

**UI data-flow anchor**

- Treat controller payload as the source of truth for live operational/manual/physical state, and train payload as the source for persistent/configuration state.
- A UI asking "what is the train's target speed right now?" reads the canonical lookahead payload from `TrainController.to_dict()` (which already embeds the live lookahead per the controller's exported v1 payload shape) and samples at distance `0`. Request handlers must not call `controller.get_lookahead()` or other controller internals directly per the "Request handlers must not read controller internals" rule in **SimulationController runtime surface**.
