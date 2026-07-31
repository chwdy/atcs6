/**
 * Fatal-conflict halt banner — extracted from app.ts.
 *
 * When the runtime is halted (failed_runtime), update() skips every train tick —
 * trains freeze at their last state (often RUNNING_MOVING / handle Px) while the
 * clock keeps advancing, and play/pause cannot recover it. Per design_new.md
 * "Fatal physical-entry conflict procedure", the halt is a gate that reopens:
 * the operator sorts the conflict out on the layout and clears it, and the
 * session carries on with every train off. Restarting the server stays available
 * as the heavier option. The helpers are pure derivations from the conflict
 * record; updateFatalBanner does the DOM + the canvas conflict-flash via the
 * passed renderer.
 */

/** Minimal renderer surface this module needs (the collision strobe). */
interface ConflictFlashRenderer {
  flashConflictTiles(tiles: Array<[number, number]>, durationMs: number): void;
}

/** Plain-English description of a fatal physical-entry conflict. */
export function describeFatalConflict(c: any): string {
  const res = `${c.resource_type} ${c.resource_id}`;
  const incoming = `Train ${c.incoming_train_id}`;
  const prior = c.prior_owner_train_id;
  switch (c.conflict_kind) {
    case 'switch_occupied_by_other_train':
      return `${incoming} tried to enter switch ${c.resource_id}, but train ${prior} already holds it.`;
    case 'occupied_by_other_train':
      return `${incoming} tried to enter block ${c.resource_id}, but train ${prior} already occupies it.`;
    case 'reserved_for_other_train':
      return `${incoming} tried to enter block ${c.resource_id}, but it is reserved for train ${prior}.`;
    case 'disabled_resource_entry':
      return `${incoming} tried to enter ${res}, which is disabled.`;
    case 'malformed_route_key':
      return `${incoming} entered switch ${c.resource_id} without a valid route key — an internal path/geometry error.`;
    default:
      return `${incoming} hit a fatal conflict at ${res}${prior ? ` (held by ${prior})` : ''}.`;
  }
}

/** Mono meta line: path ids, halt time, raw kind. */
export function fatalConflictMeta(c: any): string {
  const parts = [];
  if (c.incoming_path_id) parts.push(`${c.incoming_train_id} path: ${c.incoming_path_id}`);
  if (c.prior_owner_path_id && c.prior_owner_train_id) {
    parts.push(`${c.prior_owner_train_id} path: ${c.prior_owner_path_id}`);
  }
  if (typeof c.halted_at_sim_time === 'number') {
    parts.push(`halted at t=${c.halted_at_sim_time.toFixed(1)}s`);
  }
  parts.push(`kind: ${c.conflict_kind}`);
  return parts.join('  ·  ');
}

/** Resolve a fatal-conflict resource to its tile coordinates so the canvas
 *  can flash the collision site. A block matches every tile whose
 *  `block_section_id` is the resource id; a switch matches the tile carrying
 *  that `switch_id`. Returns [] when nothing matches (the banner still shows). */
export function resolveConflictTiles(network: any, c: any): Array<[number, number]> {
  const tiles: Array<[number, number]> = [];
  const rid = c?.resource_id;
  if (!rid || !network) return tiles;
  for (const tr of network.tracks || []) {
    if (tr.block_section_id === rid || tr.switch_id === rid) {
      tiles.push([tr.x, tr.y]);
    }
  }
  return tiles;
}

/** What this session is: a simulation, or a railway with hardware on it.
 *
 *  design_new.md "Run modes": once block occupancy is read from real detectors,
 *  the app is not simulating a layout any more, it is operating one, and the
 *  wording follows. `occupancy_is_real` is the specific test rather than the run
 *  mode as a whole, because a real-mode session that leaves occupancy simulated
 *  still runs its occupancy by calculation. */
export function runtimeNoun(network: any): string {
  return network?.occupancy_is_real ? 'Operation' : 'Simulation';
}

/** Show/hide the fatal-conflict halt banner and flash the collision site. */
export function updateFatalBanner(network: any, renderer: ConflictFlashRenderer): void {
  const banner = document.getElementById('fatal-banner');
  if (!banner) return;

  if (!network || !network.failed_runtime) {
    banner.hidden = true;
    return;
  }

  const titleEl = document.querySelector('.fatal-banner-title');
  const detailEl = document.getElementById('fatal-banner-detail');
  const metaEl = document.getElementById('fatal-banner-meta');
  const c = network.failed_runtime_conflict;
  const noun = runtimeNoun(network);
  const tail =
    ' Every train is halted. Sort it out on the layout, then clear the conflict to carry on — every train comes back off.';

  if (titleEl) {
    titleEl.textContent = `${noun} halted — fatal track conflict`;
  }
  if (detailEl) {
    detailEl.textContent = c
      ? describeFatalConflict(c) + tail
      : `A fatal track conflict halted the ${noun.toLowerCase()}.` + tail;
  }
  if (metaEl) {
    metaEl.textContent = c ? fatalConflictMeta(c) : '';
  }
  banner.hidden = false;

  // Flash the collision site on the canvas so the operator sees *where* the
  // conflict is, not just the banner text — the "whose train, and where"
  // forensic view. Re-asserted on every network refresh while the runtime
  // stays halted, so the strobe persists until the conflict is cleared, then
  // times out on its own once we stop re-asserting. Reuses the route handler's
  // red-strobe-with-X conflict flash.
  if (c) {
    const tiles = resolveConflictTiles(network, c);
    if (tiles.length > 0) {
      renderer.flashConflictTiles(tiles, 2500);
    }
  }
}
