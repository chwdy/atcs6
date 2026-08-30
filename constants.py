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

# ATO horizon-sampling constant for the lookahead two-point sample. Used by
# TrainController._ato_calculate_handle to project a future point at
# `speed_cm_s * tick_delta * LOOKAHEAD_TICKS`.
LOOKAHEAD_TICKS: int = 5

# Idle-hold release tunables per design_new.md "Idle-hold maintenance and
# release" (Planning Kernel step 66aa). A RouteRun holding a truncated
# best-effort authority (authority_end_block_id != requested_destination_block_id)
# that neither grows nor has the train moving for IDLE_HOLD_RELEASE_TICKS
# consecutive Plan ticks releases that authority through the
# pending_path_cancel_request latch so higher-priority runs can claim the held
# reservations/locks, then skips acquisition for IDLE_HOLD_COOLDOWN_TICKS ticks so
# the freed resources stay available. Counted in Plan ticks (not wall-clock
# seconds), so the release is deterministic and testable in the journey harness.
IDLE_HOLD_RELEASE_TICKS: int = 30
IDLE_HOLD_COOLDOWN_TICKS: int = 20

# How many of the most recent events `NotificationCenter` keeps before dropping
# the oldest (design_new.md "Core Runtime Loop", Runtime notifications ->
# Retention). `notification_id` keeps counting past a drop, so a gap in the ids
# means events aged out rather than that the stream lost order. Tunable, so an
# operator running a long session may raise it.
NOTIFICATION_RETENTION_ROWS: int = 10000

# How few block resources may remain ahead of the train head before Plan
# arms the next target while the current `waypoint` is still unserved
# (design_new.md "Route And RouteExecutor", Waypoint lookahead). Raising it
# makes trains hold more track ahead of themselves and brake less at
# waypoints; lowering it frees track for other trains.
WAYPOINT_LOOKAHEAD_BLOCKS: int = 3

# Delay-class on-time tolerance per design_new.md "RouteTaskRun schema"
# (delay_class). A task's actual elapsed travel time is compared with its
# expected `task_time`: elapsed below `task_time - tol` is "early", above
# `task_time + tol` is "late", otherwise "on_time". Tunable; diagnostic only.
DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS: float = 30.0

# Block occupancy detection debounce per design_new.md "Power -> Block occupancy
# detection". Each electrically-isolated block has a current detector; current
# present means a train is on the block. Real contacts flicker, so a momentary
# open circuit inside the train's span must not immediately vacate the block.
# A block is confirmed OCCUPIED after OCCUPANCY_ON_THRESHOLD consecutive
# current reads and confirmed CLEAR after OCCUPANCY_OFF_THRESHOLD consecutive
# no-current reads. The bias is fail-safe: quick to occupy, slow to clear (ride
# through dropouts). Counted in detection ticks; the runtime loop reads at 10 Hz,
# so occupancy is confirmed after ~0.2 s and cleared after ~1 s.
#
# Two reads rather than one, because a single read is one sample of a noisy
# line and a block that confirms on it inherits every glitch that line carries.
# A confirmed entry is not a private opinion of the detector: it is attributed
# to a train, it writes `Block.occupied_by`, and an occupied block is energized
# — so one bad sample moves a train in the model and puts voltage on rail. Two
# consecutive reads cost 100 ms of extra latency against a sensor lag the
# design already accepts as one tick, and they are what a detector must produce
# to be believed rather than merely heard.
#
# The clear window is sized against real dirty-rail dropouts under a MOVING
# train, which routinely outlast a few hundred milliseconds at low speed. It is
# not what protects a stopped, lit train — no debounce can, because there the
# detector reads clear indefinitely for as long as the train is parked; that
# case is handled a layer down, in `OccupancyDriver`'s lighting zone.
OCCUPANCY_ON_THRESHOLD: int = 2
OCCUPANCY_OFF_THRESHOLD: int = 10

# How many blocks a forward span correction must gain before it is reported as
# an overrun (design_new.md "Power", A large correction is reported). One block
# is the ordinary sensor-latency correction: the cap parks the head on a block's
# entry seam and the correction walks it one tile inside once that block
# confirms, so every normal block entry gains exactly one. Gaining two or more
# means the detector found the train a whole block past where the model had it,
# which the model cannot explain by latency and the operator needs to see.
OVERRUN_REPORT_MIN_BLOCKS: int = 2

# How many consecutive reads the lighting zone's deciding source — the current
# channel, or the '165 on a block that has no current channel — must call a
# block clear before the driver stops holding it occupied (design_new.md
# "Power -> Occupancy source follows drive state"). Counted in the same 10 Hz
# detection reads as the thresholds above, so this is ~3 s.
#
# This is the stopped-and-lit case the note above says no debounce can protect,
# and it is a different problem from a dropout. A locomotive drawing under its
# channel's `threshold_ma` at standby reads exactly like an empty block, so the
# window is not sized to ride anything out — it keeps the release slow enough
# that such a loco cannot cycle its block through the confirm/vacate loop
# between occupancy and drive.
#
# The window counts ONE source, not agreement between two. A '165 whose rail is
# being pulsed asserts on roughly the duty cycle (21% of reads measured at 1.5 V
# on a block with nothing on it), so letting it reset the count made the hold a
# latch no empty block ever escaped — see `OccupancyDriver`'s class docstring.
LIGHTING_CLEAR_HOLD_READS: int = 30

# How long an unidentified train that holds no blocks at all is held before it is
# retired (design_new.md "Power -> Unidentified trains", The gap). Switch tiles carry no
# detection, so an unidentified train rolling through one holds nothing for a moment; within
# this window a confirmed entry no existing train accounts for, on a block
# reachable through undetected track, is that same unidentified train continuing its run and
# keeps its `train_id`.
UNIDENTIFIED_GAP_HOLD_S: float = 5.0

# Speed calibration (design_new.md "Speed calibration"). A pass sweeps
# power_percent in CALIBRATION_POWER_STEP_PERCENT increments up to 100% and
# measures the speed the locomotive reaches at each, filling
# SpeedProfile.power_speed_curve. The 0% point needs no measurement: 0% power
# is lighting_voltage, so (0%, 0 km/h) is the curve's origin by definition.
CALIBRATION_POWER_STEP_PERCENT: int = 10

# Each step runs this many round trips across the measured block, so it
# collects twice this many crossings, half in each direction. Averaging equal
# counts each way cancels the stretch's grade: the uphill leg measures slow by
# very nearly what the downhill leg measures fast.
CALIBRATION_ROUND_TRIPS: int = 2

# A step whose measured-block entry does not confirm within this many seconds
# of `AppState.operating_time` records 0 km/h and the pass moves on. The flat
# bottom this leaves on the curve is that locomotive's real stall band.
CALIBRATION_STEP_TIMEOUT_S: float = 30.0

# Lighting-voltage calibration (design_new.md "Lighting-voltage calibration").
# One press of the panel's step control moves the candidate voltage by this
# much and puts the new value on the rails at once, so the operator sees the
# locomotive answer every press. It is small because the whole point of the
# pass is finding the last step before a creep starts.
LIGHTING_VOLTAGE_STEP_V: float = 0.1


# Hard ceiling on a single per-tick sim delta. Normal operation — including a
# few-times `simulation_speed` multiplier — stays well under this, so it is a
# pass-through in the common case and the loop runs ONE update() per tick. It
# only ever caps a multi-second hitch: the loop stalled (lock contention with
# request handlers, GC, a host that fell behind) and resumed with a huge
# accumulated wall-clock delta. Integrated in one step, such a delta overshoots
# the train's max speed and teleports the head past its authority, tripping a
# spurious ATC overspeed EB. Empirically the EB only appears around a ~3.5s
# step with a full demo trainset, so this sits below that with margin. When a
# hitch is clamped the surplus sim-time is dropped (the sim briefly runs slower
# than real time) rather than teleporting; sub-second steps are physically fine.
# design_new.md "Clocks" names constants.py as its home.
MAX_SIM_DELTA_SECONDS: float = 1.0
