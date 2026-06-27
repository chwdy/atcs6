// track-stud.ts — geometry for the buffer-stop "stud": a short bar drawn
// perpendicular to a track across a dead-end edge.
//
// Both the dead-end markers (renderer-debug.ts highlightDeadEnds) and the tunnel
// mouths (renderer-tunnels.ts) draw this same shape, so the math lives here once.
// A tunnel mouth is just a wider stud in the tunnel accent color — keeping the
// geometry shared guarantees the two read as the same family and stay matched.

export const STUD_BITS = { NS: 1, EW: 2, NE: 4, NW: 8, SE: 16, SW: 32 } as const;

export interface StudBar {
  /** One end of the bar (screen space). */
  a: { x: number; y: number };
  /** The other end of the bar (screen space). */
  b: { x: number; y: number };
  /** Bar midpoint, sitting a quarter-tile out from the tile centre toward the edge. */
  center: { x: number; y: number };
}

const PORT_TO_PATHS: Record<string, number[]> = {
  N: [STUD_BITS.NS, STUD_BITS.NE, STUD_BITS.NW],
  S: [STUD_BITS.NS, STUD_BITS.SE, STUD_BITS.SW],
  E: [STUD_BITS.EW, STUD_BITS.NE, STUD_BITS.SE],
  W: [STUD_BITS.EW, STUD_BITS.NW, STUD_BITS.SW],
};

// Direction of the bar (perpendicular to the rail) per active track path.
const PATH_PERPENDICULAR: Record<number, { dx: number; dy: number }> = {
  [STUD_BITS.NS]: { dx: 1, dy: 1 },
  [STUD_BITS.EW]: { dx: 1, dy: -1 },
  [STUD_BITS.NE]: { dx: 1, dy: 0 },
  [STUD_BITS.NW]: { dx: 0, dy: 1 },
  [STUD_BITS.SE]: { dx: 0, dy: 1 },
  [STUD_BITS.SW]: { dx: 1, dy: 0 },
};

const EDGE_POSITION: Record<string, { sx: number; sy: number }> = {
  N: { sx: 1, sy: -1 },
  E: { sx: 1, sy: 1 },
  S: { sx: -1, sy: 1 },
  W: { sx: -1, sy: -1 },
};

/**
 * Screen-space endpoints of the perpendicular stud bar at a dead-end edge.
 *
 * @param edge      cardinal edge of the dead-end ('N' | 'E' | 'S' | 'W'); a
 *                  two-char form like 'NE' uses its first char.
 * @param trackBits the tile's track_bits, used to point the bar across whichever
 *                  rail path reaches the edge (so a diagonal gets a diagonal cap).
 * @param screenX   tile-centre screen x.
 * @param screenY   tile-centre screen y.
 * @param tileSize  gridSize * scale (the bar sits a quarter-tile out from centre).
 * @param markerLen half the bar's length. Dead-end stud uses 6*scale; the tunnel
 *                  stud passes a larger value so it reads as wider.
 */
export function studBar(
  edge: string,
  trackBits: number,
  screenX: number,
  screenY: number,
  tileSize: number,
  markerLen: number,
): StudBar {
  const port = (edge.length > 1 ? edge[0] : edge).toUpperCase();
  const quarter = tileSize / 4;

  const paths = PORT_TO_PATHS[port] || [];
  let activePath: number | null = null;
  for (const bit of paths) {
    if (trackBits & bit) {
      activePath = bit;
      if (bit === STUD_BITS.NS || bit === STUD_BITS.EW) break;
    }
  }

  let dx: number;
  let dy: number;
  if (activePath && PATH_PERPENDICULAR[activePath]) {
    dx = PATH_PERPENDICULAR[activePath].dx;
    dy = PATH_PERPENDICULAR[activePath].dy;
  } else {
    dx = port === 'N' || port === 'S' ? 1 : -1;
    dy = 1;
  }
  const len = Math.hypot(dx, dy) || 1;
  dx = (dx / len) * markerLen;
  dy = (dy / len) * markerLen;

  const pos = EDGE_POSITION[port] || { sx: 0, sy: 0 };
  const cx = screenX + pos.sx * quarter;
  const cy = screenY + pos.sy * quarter;

  return {
    a: { x: cx - dx, y: cy - dy },
    b: { x: cx + dx, y: cy + dy },
    center: { x: cx, y: cy },
  };
}
