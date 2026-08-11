// bench-occupancy-mirror.ts — the Occupancy Bench's readings, on the layout.
//
// While the bench is open the runtime is not detecting: `bench/open` deactivates
// the environment and takes the device (hardware/occupancy_bench.py), so
// `poll_hardware_occupancy` returns at its first line and nothing commits
// `Block.occupied_by`. The layout therefore paints whatever occupancy was last
// committed BEFORE the handover and holds it — which reads as a canvas that
// disagrees with the panel sitting on top of it.
//
// This module closes that gap without giving the bench a writer it must not
// have. The bench is a diagnostic window, not a mode, and its whole value is
// that it reads the detectors without touching the state machine — no
// attribution, no ghost trains, no reservations. So the mirror is DISPLAY ONLY:
// the panel publishes the verdicts it is already polling at REFRESH_HZ, and the
// renderer overlays them on the block colors it draws. Nothing here reaches the
// server, and closing the bench drops the overlay, leaving the runtime's own
// committed occupancy to paint the layout again.
//
// No subscribe/notify: the app already refreshes the network (and so repaints)
// on a 200 ms interval regardless of play/pause, and `_updateNetworkState` reads
// this map on every one of those flushes. That caps mirror latency at one
// refresh period, which is far below what an operator watching a rail can see,
// and it keeps this module a plain value with no listener lifecycle to leak.

/**
 * Block id -> what the bench's last read said about it: `true` occupied,
 * `false` clear. A block the bench does not report on is absent rather than
 * `false` — "the bench has nothing to say here" and "the bench says clear" are
 * different claims, and only the second one may repaint a block.
 */
let mirror: ReadonlyMap<string, boolean> = new Map();

/**
 * Publish the bench's verdicts, or clear the overlay with `null`.
 *
 * Called on every bench poll and once more on the way out (close, hide, or the
 * session expiring under the panel). Clearing on every exit path is what keeps
 * a stale red block from outliving the window that explained it.
 */
export function setBenchOccupancyMirror(
  verdicts: Iterable<readonly [string, boolean]> | null,
): void {
  mirror = verdicts ? new Map(verdicts) : new Map();
}

/** The published verdicts. Empty whenever no bench is driving the overlay. */
export function benchOccupancyMirror(): ReadonlyMap<string, boolean> {
  return mirror;
}

/** True while the overlay has anything to say — the renderer's cheap opt-out. */
export function benchOccupancyMirrorActive(): boolean {
  return mirror.size > 0;
}
