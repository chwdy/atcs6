"""
Global runtime constants for ATCS6.

This module is the canonical home for thresholds, distances, and tunables that
appear in `design_v2.md` as global values rather than per-entity fields. Every
runtime layer imports its values from here so we never re-fork a constant again.

If a value should be configurable per entity, it belongs on that entity
instead of here.
"""

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

# How many runtime steps in a row a block must measure more than the
# short-circuit current before it is shorted (design_v2.md "When the runtime
# stops a train", A block is shorted after two reads above the short-circuit
# current). Two, because one read is one sample of a noisy channel.
SHORT_CIRCUIT_READS: int = 2

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
