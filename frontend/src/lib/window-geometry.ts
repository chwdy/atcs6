// Pure geometry math for floating chrome windows — no DOM, no classes. Four
// concerns live here: edge/corner drag-to-resize (computeFloatResize, moved
// here from editor-ui.ts where it originated as the floating inspector's
// resize math), double-click-to-fit (computeFitSize), keeping a window
// reachable when the viewport changes (clampToViewport), and choosing where a
// window opens (computeOpenPlacement). Keeping this framework-free is
// deliberate: both editor-ui.ts's TabGroup windows and lib/window-manager.ts's
// FloatingWindow share the exact same math, and unit tests exercise it without
// touching a DOM at all (this project's vitest setup has no jsdom).

/** A floating window's current edges, in viewport pixels. */
export interface FloatRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** New inline dimensions for a float resize. Only the fields the drag
 *  direction changes are present — a pure-horizontal drag returns no
 *  top/height, etc. */
export interface FloatResizeResult {
  left?: number;
  top?: number;
  width?: number;
  height?: number;
}

/** Pure resize math for a floating window's edge/corner handles. Given the
 *  grabbed direction, the window's edges at drag start, and the pointer
 *  delta, return the new inline dimensions. East/south move the right/bottom
 *  edge so the window grows from a fixed left/top; west/north move left/top
 *  so the opposite edge stays put. The window is clamped to at least
 *  minW × minH and to stay within the viewport (a `margin` gap from each
 *  side). */
export function computeFloatResize(
  dir: string,
  start: FloatRect,
  dx: number,
  dy: number,
  viewport: { width: number; height: number },
  limits: { minW: number; minH: number; margin: number }
): FloatResizeResult {
  const { minW, minH, margin } = limits;
  const out: FloatResizeResult = {};

  if (dir.includes('e')) {
    const right = Math.min(viewport.width - margin, Math.max(start.left + minW, start.right + dx));
    out.width = Math.round(right - start.left);
  } else if (dir.includes('w')) {
    const left = Math.max(margin, Math.min(start.right - minW, start.left + dx));
    out.left = Math.round(left);
    out.width = Math.round(start.right - left);
  }

  if (dir.includes('s')) {
    const bottom = Math.min(viewport.height - margin, Math.max(start.top + minH, start.bottom + dy));
    out.height = Math.round(bottom - start.top);
  } else if (dir.includes('n')) {
    const top = Math.max(margin, Math.min(start.bottom - minH, start.top + dy));
    out.top = Math.round(top);
    out.height = Math.round(start.bottom - top);
  }

  return out;
}

/** Pure fit-to-content sizing for double-click-to-auto-fit. Given the
 *  window's current top-left anchor, its content's natural (unconstrained)
 *  size, and viewport/min/max limits, returns the new width/height. Position
 *  is deliberately not part of the result: a fit grows/shrinks from the
 *  window's current top-left, the same anchor computeFloatResize's east/south
 *  directions grow from, so double-clicking to fit feels like a corner-resize
 *  to the exact content size rather than a re-center or a jump to a different
 *  screen quadrant. Clamped so a fit never exceeds the viewport (minus
 *  margin) or the caller's max, and never drops below minW/minH. */
export function computeFitSize(
  origin: { left: number; top: number },
  natural: { width: number; height: number },
  viewport: { width: number; height: number },
  limits: { minW: number; minH: number; maxW: number; maxH: number; margin: number }
): { width: number; height: number } {
  const { minW, minH, maxW, maxH, margin } = limits;
  const maxAvailW = viewport.width - origin.left - margin;
  const maxAvailH = viewport.height - origin.top - margin;
  const width = Math.round(Math.max(minW, Math.min(maxW, maxAvailW, natural.width)));
  const height = Math.round(Math.max(minH, Math.min(maxH, maxAvailH, natural.height)));
  return { width, height };
}

/** Pull a window back to a reachable position for the current viewport.
 *
 *  The rule is "fully inside when it fits, top-left margin corner when it
 *  doesn't". Only the position moves — an over-large window keeps the size the
 *  user chose, because a viewport shrink is usually temporary (a split screen,
 *  a rotated tablet) and silently resizing every window would throw that choice
 *  away for good. Pinning to the margin corner instead keeps the header — the
 *  only part that drags — on screen, so the user can always recover the window
 *  by hand.
 *
 *  Deliberately unconditional: the caller does not check for overflow first.
 *  An earlier version only pulled windows in when they overflowed the right or
 *  bottom edge, which silently left a window stranded at a negative left/top
 *  after a viewport grew and then shrank again. */
export function clampToViewport(
  rect: { left: number; top: number; width: number; height: number },
  viewport: { width: number; height: number },
  limits: { margin: number }
): { left: number; top: number } {
  const { margin } = limits;
  const maxLeft = viewport.width - rect.width - margin;
  const maxTop = viewport.height - rect.height - margin;
  return {
    left: maxLeft >= margin ? Math.round(Math.min(Math.max(rect.left, margin), maxLeft)) : margin,
    top: maxTop >= margin ? Math.round(Math.min(Math.max(rect.top, margin), maxTop)) : margin,
  };
}

/** Area of the intersection of two rects, 0 when they don't overlap. */
function overlapArea(a: FloatRect, b: FloatRect): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return w > 0 && h > 0 ? w * h : 0;
}

/** Where a panel should open so it doesn't land on top of the thing the user
 *  is looking at.
 *
 *  `avoid` is the region to keep clear in viewport coordinates — the drawn
 *  track layout's bounding box for a window, the edited tiles' footprint for an
 *  anchored dialog. `bounds` is the area placement may use, already inset for
 *  the chrome a panel must not cover (the floating tool rail on the left, the
 *  app header above). `preferred` is where the caller would put the panel if
 *  nothing were in the way; the four candidates are displacements of it.
 *
 *  Candidates are tried in order — right of `avoid`, left of it, below, above —
 *  and the first that clears `avoid` entirely wins. When nothing clears it (the
 *  layout fills the viewport), the candidate overlapping `avoid` least wins
 *  instead, so a cramped viewport degrades to "least in the way" rather than
 *  falling back to a fixed corner. Right comes first because the left of the
 *  canvas is where the tool rail and the layout's own origin sit; a window on
 *  the right also matches where the docked inspector lives, so floating and
 *  docked read as the same panel in the same place.
 *
 *  Sideways candidates keep `preferred.top` and vertical ones keep
 *  `preferred.left`, so a panel that wants to stay level with what it edits (an
 *  anchored dialog) slides sideways rather than jumping to a screen corner.
 *
 *  With no `avoid` (no layout drawn yet, or an unknown camera), `preferred`
 *  comes back clamped into bounds and nothing else happens. */
export function computeOpenPlacement(
  size: { width: number; height: number },
  bounds: FloatRect,
  avoid: FloatRect | null,
  limits: { gap: number; preferred?: { left: number; top: number } }
): { left: number; top: number } {
  const { gap } = limits;
  const preferred = limits.preferred ?? { left: bounds.left, top: bounds.top };
  const clampInBounds = (left: number, top: number): { left: number; top: number } => ({
    left: Math.round(Math.min(Math.max(left, bounds.left), Math.max(bounds.left, bounds.right - size.width))),
    top: Math.round(Math.min(Math.max(top, bounds.top), Math.max(bounds.top, bounds.bottom - size.height))),
  });

  if (!avoid) {
    return clampInBounds(preferred.left, preferred.top);
  }

  const candidates = [
    { left: avoid.right + gap, top: preferred.top }, // right of it
    { left: avoid.left - gap - size.width, top: preferred.top }, // left of it
    { left: preferred.left, top: avoid.bottom + gap }, // below it
    { left: preferred.left, top: avoid.top - gap - size.height }, // above it
  ].map((c) => clampInBounds(c.left, c.top));

  let best = candidates[0];
  let bestOverlap = Infinity;
  for (const c of candidates) {
    const area = overlapArea(
      { left: c.left, top: c.top, right: c.left + size.width, bottom: c.top + size.height },
      avoid
    );
    if (area === 0) {
      return c; // clears the layout outright — first such candidate wins
    }
    if (area < bestOverlap) {
      best = c;
      bestOverlap = area;
    }
  }
  return best;
}
