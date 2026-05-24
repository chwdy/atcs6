> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Design Direction

**Ownership split**

- `TrainController` owns live operational state: ATC/ATO/manual mode, handle position, future manual `power_percent`-mode inputs, and the canonical live lookahead data series. "Target speed" and "speed-limit reason" MUST NOT be stored as standalone fields anywhere; they are the value and reason metadata at position 0 of the lookahead series.
- `TrainController` also owns live power/cab state: the canonical `state` machine (which includes `OFF` for power off), active cab selection, and any live direction-selection inputs tied to that active cab. The convenience read `power_on` is derived from `state != "OFF"`. After the runtime 1:1 `Train` <-> `TrainController` pairing is established, those values are not re-synced from `Train` on every tick.
- `Train` owns persistent/configuration state only: identity, consist, schedule/route assignment, and non-live train configuration. Live position/span, live speed, ATO/ATC/path-runtime state, power/cab state, and operational state are not mirrored on `Train`. Initial live placement comes through `SimulationController.add_train(...)`: it constructs the `Train`, creates or obtains the `TrainController`, derives the initial span from the placement payload, and writes the initial `TrainController.physics_sync.current_span` before the first simulation tick. The frontend/API input shape (`id`, optional `position: {x, y}`, `direction`, `max_speed`, `length`, and other train-configuration fields) is unchanged. Bootstrap data must enter through `add_train(...)` or explicit controller commands.
- The new manual model uses handle plus power-percentage; there is no `manual_target_speed` field.
- Manual control has two controller-owned submodes:
  - `simulation`: user drives with handle notches; the handle command flows to `TrainPhysics`, which integrates it into speed change over time.
  - `power_percent`: user drives with percentage power; the controller passes raw percentage power to `TrainPhysics`, which immediately converts it via train/profile calibration data such as `SpeedProfile.power_speed_curve` into both track voltage and the calibrated current speed when no braking override is active.
- `TrainPhysics` and `BlockPowerController` MUST NOT decide manual-mode policy. They execute whichever command the controller produces and perform command-to-speed conversion and voltage emission.
- Percentage-power mapping data lives in `SpeedProfile.power_speed_curve` as part of train calibration and is read by physics.

## Layered Architecture

**Layered responsibilities**

- **`PathDispatcher`** is the sole owner of dispatcher path records and movement authority. It acquires future blocks, locks future switches, and exposes its current authority geometry to the controller as a read-only projection. The full path lifecycle, the `paths_by_id` registry, the `active_path_id_by_train(train_id)` derived read, the same-train rejection in `create_path(...)`, and the resource-acquisition / release rules are all canonical in [200-path-dispatcher.md](200-path-dispatcher.md).
- **`TrainController`** executes the path plan by calculating ATC safety limits, ATO/manual commands, and the active handle or power command. It reads dispatcher authority through the live property `TrainController.path_projection.received_path_plan` (no controller-side copy; no setter), brakes to the current authority boundary using its canonical safety margin, and runs the **Reactive speed-based state evaluation** synchronously inside `TrainController.on_physics_updated(span_update)`. Full controller surface in [310-train-controller.md](310-train-controller.md); progress-report and `received_path_plan` contracts in [030-core-loop.md](030-core-loop.md) and [200-path-dispatcher.md](200-path-dispatcher.md).
- **`TrainPhysics`** is an executor only; it MUST NOT decide manual-mode policy. It computes one `span_update` per crossed boundary plus a final no-boundary callback per tick, invokes `TrainController.on_physics_updated(span_update)` for each, and returns the collected `List[ControllerProgressReport]` to `SimulationController`. Full physics contract in [320-train-physics.md](320-train-physics.md).
- The dispatcher-owned path record is the single source of truth for controller authority while it is active. Cancelling or replacing a path in `PathDispatcher` is sufficient to give the controller a clean slate; the controller's `received_path_plan` property automatically reflects the change on the next read.
