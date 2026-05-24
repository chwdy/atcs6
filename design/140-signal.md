> Part of the [ATCS6 design spec](README.md). Cross-cutting: [Core loop & latches](030-core-loop.md) · [Units & time](040-units.md) · [Glossary](010-glossary.md).

## Signals

**Signals**

- Signals remain display-only. CAUTION handling moves to `TrainController.CAUTION_SPEED_LIMIT_KMH` (enforced via the lookahead / ATC path), not to signal aspects. `RailwayNetwork.update_signals_from_blocks()` still produces aspect values for the UI, but no controller or physics layer consumes them as a speed restriction.
