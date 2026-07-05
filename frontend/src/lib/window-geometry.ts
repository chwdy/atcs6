// Pure geometry math for floating chrome windows — no DOM, no classes. Two
// concerns live here: edge/corner drag-to-resize (computeFloatResize, moved
// here from editor-ui.ts where it originated as the floating inspector's
// resize math) and double-click-to-fit (computeFitSize). Keeping this
// framework-free is deliberate: both editor-ui.ts's TabGroup windows and
// lib/window-manager.ts's FloatingWindow share the exact same math, and unit
// tests exercise it without touching a DOM at all.

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
