> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## SpeedProfile

`SpeedProfile` carries motion-rate data plus per-train calibration in the vocabulary the rest of the system should use directly: voltage, power percent, and speed. The calibration defines bidirectional mappings between `track_voltage` and `power_percent`, and between `power_percent` and calibrated `speed_kmh`. Keep those mappings explicit in the doc instead of introducing extra terminology.

- Keep these fields on `SpeedProfile`:
  - `profile_id`: unique profile identifier.
  - `name`: display name for UI/debug/profile selection.
  - `startup_voltage`: minimum `track_voltage` that produces movement. Below this threshold the train remains stopped.
  - `max_voltage`: top of the usable `track_voltage` range for the first mapping stage.
  - `power_speed_curve`: stored calibration mapping `(power_percent, speed_kmh)` for the second stage.
  - `lighting_voltage`: voltage used for stopped lit states.
  - `acceleration_rate`: service acceleration rate in `km/h/s`.
  - `deceleration_rate`: service braking rate in `km/h/s`.
  - `emergency_brake_rate`: emergency-brake deceleration rate in `km/h/s`.
  - `friction_rate`: passive/coasting deceleration rate in `km/h/s`.
  - `operational_decel_ratio`: comfort-braking fraction used when ATO builds operational braking curves.
  - `air_resistance_enabled`: whether the drag model is active.
  - `drag_coefficient`: drag strength used by the drag model.
  - `reference_speed_kmh`: reference speed used to scale the drag model.

**SpeedProfile constraint**

- `SpeedProfile` is still not a speed limit object under the desired model. It expresses motion-rate data and per-train calibration (`startup_voltage`, `max_voltage`, `power_speed_curve`), not any form of speed ceiling. Train maximum speed and block maximum speed remain separate, independently enforced constraints.

**Speed-profile addition**

- Keep the desired-model calibration wording in three explicit values: `track_voltage`, `power_percent`, and `speed_kmh`.
- The calibration has two bidirectional mappings and both should be stated directly in the doc:
  1. `track_voltage <-> power_percent`
  2. `power_percent <-> speed_kmh`
- `startup_voltage` and `max_voltage` define the voltage range used by the first mapping. Below `startup_voltage`, the train does not move. `max_voltage` is the top of the usable range. In v1, `track_voltage <-> power_percent` is linear across that usable range, so reversing from `power_percent` back to `track_voltage` uses the same linear interpolation.
- `power_speed_curve` is the stored `(power_percent, speed_kmh)` mapping used by the second step. `power_percent` is the intermediate normalized control value between voltage and speed; it is not a second name for either of them. In v1 this may be linear by default, but it is still stored explicitly as profile calibration data rather than inferred ad hoc elsewhere. Reverse lookup from `speed_kmh` to `power_percent` uses the inverse interpolation of this same stored curve.
- The motion-rate fields (`acceleration_rate`, `deceleration_rate`) govern ATO, manual `simulation` handle integration, service/emergency braking, and coasting; they do not shape manual `power_percent` response (that submode applies the calibrated speed immediately, see TrainPhysics).
- Every train should have a default `SpeedProfile` carrying these calibration fields so the `power_percent` manual mode always has a defined voltage/power/speed mapping, even before per-train tuning is customized.
