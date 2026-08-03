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

# Idle-hold release tunables per design_new.md "Idle-hold maintenance and
# release" (Planning Kernel step 66aa). A RouteRun holding a truncated
# best-effort authority (authority_end_block_id != requested_destination_block_id)
# that neither grows nor has the train moving for IDLE_HOLD_RELEASE_TICKS
# consecutive Phase-1 ticks releases that authority through the
# pending_path_cancel_request latch so higher-priority runs can claim the held
# reservations/locks, then skips acquisition for IDLE_HOLD_COOLDOWN_TICKS ticks so
# the freed resources stay available. Counted in Phase-1 ticks (not wall-clock
# seconds), so the release is deterministic and testable in the journey harness.
IDLE_HOLD_RELEASE_TICKS: int = 30
IDLE_HOLD_COOLDOWN_TICKS: int = 20

# How many of the most recent events `NotificationCenter` keeps before dropping
# the oldest (design_new.md "Core Runtime Loop", Runtime notifications ->
# Retention). `notification_id` keeps counting past a drop, so a gap in the ids
# means events aged out rather than that the stream lost order. Tunable, so an
# operator running a long session may raise it.
NOTIFICATION_RETENTION_ROWS: int = 10000

# Delay-class on-time tolerance per design_new.md "RouteTaskRun schema"
# (delay_class). A task's actual elapsed travel time is compared with its
# expected `task_time`: elapsed below `task_time - tol` is "early", above
# `task_time + tol` is "late", otherwise "on_time". Tunable; diagnostic only.
DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS: float = 5.0

# Block occupancy detection debounce per design_new.md "Power -> Block occupancy
# detection". Each electrically-isolated block has a current detector; current
# present means a train is on the block. Real contacts flicker, so a momentary
# open circuit inside the train's span must not immediately vacate the block.
# A block is confirmed OCCUPIED after OCCUPANCY_ON_THRESHOLD consecutive
# current reads and confirmed CLEAR after OCCUPANCY_OFF_THRESHOLD consecutive
# no-current reads. The bias is fail-safe: quick to occupy, slow to clear (ride
# through dropouts). Counted in detection ticks; the runtime loop reads at 10 Hz,
# so occupancy is confirmed on the first read and cleared after ~1 s.
#
# The clear window is sized against real dirty-rail dropouts under a MOVING
# train, which routinely outlast a few hundred milliseconds at low speed. It is
# not what protects a stopped, lit train — no debounce can, because there the
# detector reads clear indefinitely for as long as the train is parked; that
# case is handled a layer down, in `OccupancyDriver`'s lighting zone.
OCCUPANCY_ON_THRESHOLD: int = 1
OCCUPANCY_OFF_THRESHOLD: int = 10

# How many consecutive reads BOTH occupancy sources must agree a lighting-zone
# block is clear before the driver stops holding it occupied (design_new.md
# "Power -> Occupancy source follows drive state"). Counted in the same 10 Hz
# detection reads as the thresholds above, so this is ~3 s.
#
# This is the stopped-and-lit case the note above says no debounce can protect,
# and it is a different problem from a dropout. A lit train parked in the
# lighting zone reads clear on both sources indefinitely, so the window is not
# sized to ride anything out — it is sized against the detector flicker that the
# rail drive itself causes. Any single occupied read from either source resets
# the count, so a flickering detector delays the release instead of racing it,
# and the confirm/vacate loop between occupancy and drive cannot re-form.
LIGHTING_CLEAR_HOLD_READS: int = 30

# How long an unidentified train that holds no blocks at all is held before it is
# retired (design_new.md "Power -> Unidentified trains", The gap). Switch tiles carry no
# detection, so an unidentified train rolling through one holds nothing for a moment; within
# this window a confirmed entry no existing train accounts for, on a block
# reachable through undetected track, is that same unidentified train continuing its run and
# keeps its `train_id`.
UNIDENTIFIED_GAP_HOLD_S: float = 5.0

# Speed calibration (design_new.md "Speed calibration"). A calibration pass
# commands a fixed sweep of CALIBRATION_VOLTAGE_STEPS track-voltage steps from
# the profile's lighting_voltage to its max_voltage and measures the speed the
# train reaches at each, filling the SpeedProfile curves. In manual mode the
# operator marks each lap and calibration averages CALIBRATION_MANUAL_LAPS laps
# per step so one noisy lap does not set a point.
CALIBRATION_VOLTAGE_STEPS: int = 8
CALIBRATION_MANUAL_LAPS: int = 3
