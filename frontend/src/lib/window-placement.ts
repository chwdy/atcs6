// window-placement.ts — the DOM half of "don't open on top of the layout".
//
// window-geometry.ts owns the rule (computeOpenPlacement, pure and unit-tested);
// this module owns the two measurements that rule needs and cannot make itself:
// where the drawn track layout currently sits on screen, and how much of the
// viewport the app's own floating chrome has already claimed.
//
// Mirrors App._canvasChromeInsets, which does the same live-chrome measurement
// for Camera.fitToContent — Camera stays free of DOM knowledge for exactly the
// same reason FloatingWindow does.

import { computeOpenPlacement, type FloatRect } from './window-geometry';

/** Gap between the layout (or the chrome) and a panel placed beside it. */
const PLACEMENT_GAP = 12;
/** Inset from the viewport edges that placement will not cross. */
const PLACEMENT_MARGIN = 8;

/** Corner offsets of one grid tile — tile (x,y) spans (x,y)…(x+1,y+1). */
const TILE_CORNERS: ReadonlyArray<readonly [number, number]> = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];

/** The screen box of a set of grid tiles, in viewport pixels, or null when it
 *  can't be measured (no camera, no canvas, no tiles).
 *
 *  Projects every tile's four corners the way Camera.fitToContent does — a real
 *  network is sparse, so its grid bbox's diamond is mostly empty space and would
 *  over-report the region worth avoiding. The main canvas is sized in CSS pixels
 *  (renderer.ts does not multiply by devicePixelRatio), so camera screen
 *  coordinates plus the canvas rect land directly in viewport space, the same
 *  conversion popover-anchor.ts makes. */
export function tilesScreenRect(tiles: ReadonlyArray<{ x: number; y: number }>): FloatRect | null {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return null; // no-DOM context (unit tests) — nothing to measure
  }
  const camera = (window as any).app?.renderer?.camera;
  const canvas = document.getElementById('rail-canvas') as HTMLCanvasElement | null;
  if (!camera || !canvas || !tiles || tiles.length === 0) {
    return null;
  }

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const t of tiles) {
    if (typeof t?.x !== 'number' || typeof t?.y !== 'number') {
      continue;
    }
    for (const [dx, dy] of TILE_CORNERS) {
      const px = camera.gridToScreenX(t.x + dx, t.y + dy);
      const py = camera.gridToScreenY(t.x + dx, t.y + dy);
      if (px < minX) minX = px;
      if (px > maxX) maxX = px;
      if (py < minY) minY = py;
      if (py > maxY) maxY = py;
    }
  }
  if (!isFinite(minX) || !isFinite(minY)) {
    return null;
  }

  const rect = canvas.getBoundingClientRect();
  return {
    left: rect.left + minX,
    top: rect.top + minY,
    right: rect.left + maxX,
    bottom: rect.top + maxY,
  };
}

/** The whole drawn layout's on-screen bounding box, or null when nothing is
 *  drawn (empty scene, renderer not up yet). */
export function layoutScreenRect(): FloatRect | null {
  if (typeof window === 'undefined') {
    return null;
  }
  const tracks = (window as any).app?.currentNetwork?.tracks;
  return Array.isArray(tracks) ? tilesScreenRect(tracks) : null;
}

/** The region a floating panel may occupy: the viewport, inset past every piece
 *  of chrome that owns its own space — the app header above, the floating tool
 *  rail on the left, and the docked inspector on the right. All measured live,
 *  so a collapsed rail, a hidden header, or an undocked inspector gives the
 *  space straight back.
 *
 *  The docked inspector is the one that matters most: it holds a real layout
 *  slot on the right, so a panel that ran to the viewport edge would sit on top
 *  of it. Only the *docked* one counts — once it floats it is a window like any
 *  other, and windows are allowed to overlap each other. */
export function placementBounds(): FloatRect {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    // No-DOM context: a nominal box, so callers stay total rather than throwing.
    return { left: 0, top: 0, right: 0, bottom: 0 };
  }
  const bounds: FloatRect = {
    left: PLACEMENT_MARGIN,
    top: PLACEMENT_MARGIN,
    right: window.innerWidth - PLACEMENT_MARGIN,
    bottom: window.innerHeight - PLACEMENT_MARGIN,
  };

  const rail = document.querySelector('.sidebar:not(.collapsed)') as HTMLElement | null;
  if (rail) {
    const railRect = rail.getBoundingClientRect();
    if (railRect.width > 0) {
      bounds.left = Math.max(bounds.left, railRect.right + PLACEMENT_GAP);
    }
  }

  const header = document.querySelector('.app-header') as HTMLElement | null;
  if (header) {
    const headerRect = header.getBoundingClientRect();
    if (headerRect.height > 0) {
      bounds.top = Math.max(bounds.top, headerRect.bottom + PLACEMENT_GAP);
    }
  }

  const dockedInspector = document.querySelector(
    '.secondary-sidebar.visible:not(.floating-window):not(.user-hidden)'
  ) as HTMLElement | null;
  if (dockedInspector) {
    const inspectorRect = dockedInspector.getBoundingClientRect();
    if (inspectorRect.width > 0) {
      bounds.right = Math.min(bounds.right, inspectorRect.left - PLACEMENT_GAP);
    }
  }

  return bounds;
}

/** Where to open a panel of `size` so it clears the drawn layout. `avoid`
 *  defaults to the whole layout; pass a tighter rect (the tiles being edited)
 *  when only that part matters. `preferred` keeps a panel level with what it
 *  edits — omitted, placement starts from the bounds' top-left corner. */
export function placeClearOfLayout(
  size: { width: number; height: number },
  opts: { avoid?: FloatRect | null; preferred?: { left: number; top: number } } = {}
): { left: number; top: number } {
  const avoid = opts.avoid !== undefined ? opts.avoid : layoutScreenRect();
  return computeOpenPlacement(size, placementBounds(), avoid, {
    gap: PLACEMENT_GAP,
    preferred: opts.preferred,
  });
}
