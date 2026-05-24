> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Power

**Power-controller role**

- Keep `BlockPowerController` focused on the software-to-hardware boundary only: it accepts a voltage number and applies it to block outputs.
- `TrainPhysics` computes `track_voltage` from the current authoritative operating input for the active mode: `speed_kmh -> power_percent -> track_voltage` in ATO and manual `simulation` mode, or `manual_power_percent -> track_voltage` plus the calibrated absolute `speed_kmh` command in manual `power_percent` mode. In manual `power_percent` mode without a braking override, the calibrated speed is effective immediately in `PhysicsSpanUpdate.current_speed_kmh`. `BlockPowerController` applies that already-computed voltage to the hardware-facing block outputs.
- Do not let it own manual-mode state or read `manual_target_speed`.
