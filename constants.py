"""
Global runtime constants for ATCS6.

This module is the canonical home for thresholds, distances, and tunables that
appear in `design_new.md` as global values rather than per-entity fields. Every
runtime layer (controller, physics, dispatcher, route executor, block manager)
imports its values from here so we never re-fork a constant again.

If a value should be configurable per-train or per-route, it belongs on the
relevant entity (`SpeedProfile`, `Route`, `RouteTask`, ...) instead of here.
"""

# Global "considered stopped" speed threshold per design_new.md "Units contract".
# Used by TrainController.is_stopped(), the post-physics usable-authority stop
# check, command-acceptance gates (set_cab, set_mode, power_off_cmd,
# release_emergency_cmd), and any other layer that needs the canonical stopped
# predicate. This is distinct from the ATC zero-speed limit, which is a separate
# clearance value.
STOPPED_SPEED_THRESHOLD_KMH: float = 0.1

# Controller safety margin globals per design_new.md "Safety margin globals".
# Both are owned by TrainController lookahead and are not authored on routes,
# paths, or per-block fields.
TRACK_END_SAFETY_MARGIN_CM: float = 25.0
PLATFORM_SAFETY_MARGIN_CM: float = 5.0

# Flank protection global per design_new.md "Flank Protection Computation".
# Used at switch placement time to decide which neighbours require mutual
# locking.
FLANK_PROTECTION_DISTANCE_CM: float = 5.0

# CAUTION speed cap applied by TrainController lookahead when it samples a block
# whose computed Block.state is CAUTION. Owned by the controller; not a signal
# property.
CAUTION_SPEED_LIMIT_KMH: float = 40.0

# Run-number registry constant per design_new.md "Route-run state":
# `run_number = route_registry_index * COPY_COUNT_BASE + run_count_for_route`.
# 1_000_000 leaves room for ~1M runs per route before the next route slot.
COPY_COUNT_BASE: int = 1_000_000

# ATO horizon-sampling constant for the lookahead two-point sample. Used by
# TrainController._ato_calculate_handle to project a future point at
# `speed_cm_s * tick_delta * LOOKAHEAD_TICKS`.
LOOKAHEAD_TICKS: int = 5
