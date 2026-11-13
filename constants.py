"""
Global runtime constants for ATCS6.

This module is the canonical home for thresholds, distances, and tunables that
appear in `design_v2.md` as global values rather than per-entity fields. Every
runtime layer imports its values from here so we never re-fork a constant again.

If a value should be configurable per entity, it belongs on that entity
instead of here.
"""

# v1 "considered stopped" speed threshold. v2 has no train speed (a train is
# stopped while its knob is at 0 V); only `runtime_config.py`'s docstring still
# names this value.
STOPPED_SPEED_THRESHOLD_KMH: float = 0.1

# v1 controller safety margins. No code reads them.
TRACK_END_SAFETY_MARGIN_CM: float = 25.0
PLATFORM_SAFETY_MARGIN_CM: float = 5.0

# v1 stop-arrival tolerance. No code reads it.
RESERVATION_STOP_ARRIVAL_TOLERANCE_CM: float = 2.0

# v1 flank protection distance. No code reads it.
FLANK_PROTECTION_DISTANCE_CM: float = 5.0

# CAUTION speed cap applied by TrainController lookahead when it samples a block
# whose computed Block.state is CAUTION. Owned by the controller; not a signal
# property.
CAUTION_SPEED_LIMIT_KMH: float = 40.0

# ATO horizon-sampling constant for the lookahead two-point sample. Used by
# TrainController._ato_calculate_handle to project a future point at
# `speed_cm_s * tick_delta * LOOKAHEAD_TICKS`.
LOOKAHEAD_TICKS: int = 5

# v1 braking-distance reservation margins. No code reads them.
BRAKING_LOCK_ACCEL_HEADROOM_KMH: float = 20.0
BRAKING_LOCK_LIMIT_MARGIN_KMH: float = 5.0

# How many of the most recent events `NotificationCenter` keeps before dropping
# the oldest (design_v2.md "Runtime notifications", Retention).
# `notification_id` keeps counting past a drop, so a gap in the ids
# means events aged out rather than that the stream lost order. Tunable, so an
# operator running a long session may raise it.
NOTIFICATION_RETENTION_ROWS: int = 10000

# Detector reading log files (design_v2.md "Detector reading log", File size
# limits).
# A file that has grown past DETECTOR_READING_LOG_MAX_FILE_BYTES is closed and
# the next line starts a new one, which opens by listing every block. Opening a
# file deletes the oldest files until the folder holds no more than
# DETECTOR_READING_LOG_MAX_TOTAL_BYTES. A 10-minute run of demo_6 (163 blocks,
# 17 trains) wrote about 1 MB under v1, so the budget keeps days of runs.
DETECTOR_READING_LOG_MAX_FILE_BYTES: int = 20 * 1024 * 1024
DETECTOR_READING_LOG_MAX_TOTAL_BYTES: int = 200 * 1024 * 1024

# v1 route-task delay tolerance. No code reads it.
DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS: float = 30.0

# Block occupancy detection debounce per design_v2.md "Occupancy detection".
# Each electrically-isolated block has a current detector; current present
# means a train is on the block. Real contacts flicker, so a momentary open
# circuit under a train must not immediately make the block read empty.
# A block is confirmed OCCUPIED after OCCUPANCY_ON_THRESHOLD consecutive
# current reads and confirmed CLEAR after OCCUPANCY_OFF_THRESHOLD no-current
# reads. While a confirmed block counts toward clear, a lone current read pauses
# the count and only OCCUPANCY_ON_THRESHOLD consecutive current reads restart
# it, so one stray sample on an empty powered block cannot hold the block. The
# bias is fail-safe: quick to occupy, slow to clear (ride through dropouts). Counted in detection ticks; the runtime loop reads at 10 Hz,
# so occupancy is confirmed after ~0.2 s and cleared after ~1 s.
#
# Two reads rather than one, because a single read is one sample of a noisy
# line and a block that confirms on it inherits every glitch that line carries.
# A confirmed entry is not a private opinion of the detector: it gives the
# block to a train (design_v2.md "Trains"), so one bad sample puts a train on
# the page, or onto a block, where there is none. Two
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

# v1 overrun-report threshold for its span correction. No code reads it; v2
# reports an overrun its own way (design_v2.md "When a train overruns").
OVERRUN_REPORT_MIN_BLOCKS: int = 2

# How many consecutive reads the lighting zone's deciding source — the current
# channel, or the '165 on a block that has no current channel — must call a
# block clear before the driver stops holding it occupied (design_v2.md
# "Occupancy source and drive zones", The lighting hold). Counted in the same
# 10 Hz detection reads as the thresholds above, so this is ~3 s.
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

# v1 hold for an unidentified train with no blocks. No code reads it; v2's
# hold time is the runtime setting `hold_time_s`.
UNIDENTIFIED_GAP_HOLD_S: float = 5.0

# v1 speed calibration and speed profile values, down to the Default profile
# below. v2 has no speed calibration and no speed profiles, and no code reads
# these.
CALIBRATION_VOLTAGE_STEP_V: float = 2.0

# The narrowing halves the gap between the lowest step that passed and the step
# that failed below it (or the starting voltage when none failed) until the gap
# is no more than this; the lowest step that passed is then the curve's first
# point. A filled step needs its two neighbours more than twice this apart.
CALIBRATION_MIN_VOLTAGE_GAP_V: float = 0.25

# The pass measures the voltage halfway between two neighbouring passed steps
# whose speeds differ by more than this fraction of the top step's speed, so the
# curve has its points where the speed changes fast.
CALIBRATION_FILL_SPEED_FRACTION: float = 0.15

# A step that measures faster than the step above it by no more than this
# fraction of that step's speed is the same speed: the step records the step
# above's speed, so the curve runs flat there. A light motor often reaches its
# top speed well below 100% power, and on that flat stretch a few hundredths of
# a second of detector timing decide which step reads faster.
CALIBRATION_SAME_SPEED_FRACTION: float = 0.10

# Each step runs this many round trips across the measured block, so it
# collects twice this many crossings, half in each direction, and takes their
# median. Equal counts each way cancel the stretch's grade: the uphill leg
# measures slow by very nearly what the downhill leg measures fast.
CALIBRATION_ROUND_TRIPS: int = 2

# A step whose measured-block entry does not confirm within this many seconds
# of `AppState.operating_time` records 0 km/h, as does every step below it, and
# the sweep ends. The flat bottom this leaves on the curve is that locomotive's
# real stall band. The same limit bounds driving the train off the measured
# block after a crossing.
CALIBRATION_STEP_TIMEOUT_S: float = 30.0

# The slowest crossing speed the pass times, in scale km/h. A crossing gets the
# time the measured block's length takes at this speed (the crossing window);
# one that has not reached the far outer block by then stopped inside the
# stretch, and ends the sweep like a step that does not move the train.
CALIBRATION_MIN_CROSSING_SPEED_KMH: float = 2.0

# Each leg starts at half the step's power for this many seconds before the
# step's power, and each stop after a crossing holds half the step's power for
# this long before cutting it, so the locomotive never takes a full step of
# track voltage at once.
CALIBRATION_HALF_POWER_S: float = 0.5

# Each end of the calibration stretch takes run-off blocks past its outer block
# until it holds the train's entered length plus this many centimetres, then
# one block more. The margin covers the
# detector's lag on the far block's entry and a length entered a little short.
CALIBRATION_RUNOFF_MARGIN_CM: float = 5.0

# How many detector reads a standing train is given for the measured block to
# read clear before the pass decides the train stopped with its tail still on
# it. Below `current_sense_min_drive_v` a block holds its presence for
# LIGHTING_CLEAR_HOLD_READS clear reads, then takes OCCUPANCY_OFF_THRESHOLD more
# to confirm clear; ten reads more absorb a lone noisy read pausing the count.
CALIBRATION_OFF_B_WAIT_READS: int = LIGHTING_CLEAR_HOLD_READS + OCCUPANCY_OFF_THRESHOLD + 10

# When a pass opens with the train not standing in an outer block, it first
# drives the train at this voltage into the nearer one. A fixed voltage,
# because a target speed and a power percent are both read through the curve
# the pass is about to measure.
CALIBRATION_MOVE_VOLTAGE_V: float = 6.0

# When a measured crossing is capped by the leg's own reservation end (the
# stretch is too short for this step's speed even at the zero reservation-end
# standoff) or by a repeated conflict, that crossing is discarded and
# re-measured. This many consecutive discarded crossings of one step ends the
# pass with a "stretch too short" report, so a too-short stretch fails rather
# than shuttling forever.
CALIBRATION_CAP_RETRY_LIMIT: int = 4

# A speed pass opens with two trial stages in which the operator sets the
# voltage by hand. The lighting trial starts here when the train's profile sets
# no lighting voltage, and the panel steps the trial voltage by
# CALIBRATION_TRIAL_STEP_V. The ATO minimum test uses the same step.
CALIBRATION_LIGHTING_TRIAL_START_V: float = 1.5
CALIBRATION_TRIAL_STEP_V: float = 0.1

# The ATO minimum test marks its test voltage steady once this many round trips
# in a row have finished at it with no stalled crossing.
ATO_MINIMUM_TEST_ROUND_TRIPS: int = 3

# The Default profile's values. `DEFAULT_SEEDED_MAX_VOLTAGE_V`
# is the top of the usable `track_voltage` range. `DEFAULT_SEEDED_STARTING_VOLTAGE_V`
# is the Default `starting_voltage`. The Default curve is the straight line from
# 0 V -> 0 km/h to `max_voltage` -> `DEFAULT_SEEDED_TOP_SPEED_KMH`, with its
# first point at `DEFAULT_SEEDED_FIRST_POINT_VOLTAGE_V`, above the starting
# voltage; a copy into a train's instance profile draws the line to the train's
# `max_speed` instead. The Default profile sets no `lighting_voltage`.
DEFAULT_SEEDED_MAX_VOLTAGE_V: float = 12.0
DEFAULT_SEEDED_STARTING_VOLTAGE_V: float = 1.0
DEFAULT_SEEDED_FIRST_POINT_VOLTAGE_V: float = 1.5
DEFAULT_SEEDED_TOP_SPEED_KMH: float = 100.0


# Commissioning session bounds (design_v2.md "Hardware REST API", Commissioning
# limits live in `constants.py`).
# The commissioning `open` body carries `timeout_seconds` with the doc's stated
# bound `0 < timeout_seconds <= COMMISSION_MAX_SESSION_SECONDS`: it is the idle
# window after which an abandoned session is reclaimed and its outputs dropped to
# the safe state. This ceiling only rejects an absurd request; a real bench
# session — a person wiring and poking at a rig — runs minutes, well under an
# hour. It is generous for any genuine session, and finite enough that a
# forgotten one cannot request an unbounded hold on the controller. A session
# opened with no `timeout_seconds` keeps the default idle window in
# `hardware/commission.py` (IDLE_TIMEOUT_S).
COMMISSION_MAX_SESSION_SECONDS: float = 3600.0

# The longest the commissioning `pulse` action may drive a shift-out bit is
# MAX_COIL_PULSE_S in hardware/catalog.py: any such bit may be wired to a coil.
