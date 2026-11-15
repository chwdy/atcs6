// track-path.ts — traces a line of track on the canvas with rounded bends.
//
// Everything that follows the track uses this one function: the grey track,
// a block's status line and a train's border. They all take the same points
// and the same radius, so their bends lie on top of each other.

import { TRACK_BEND_RADIUS } from '../lib/constants';

export type PathPoint = { x: number; y: number };

/** Add `points` to the current canvas path as one line, each bend drawn as
 *  an arc. `scale` is the renderer's zoom. An arc uses at most half of the
 *  shorter piece of line on either side of its bend, so two bends close
 *  together never overlap. The caller calls beginPath() and stroke(). */
export function traceTrackLine(
  ctx: CanvasRenderingContext2D,
  points: PathPoint[],
  closed: boolean,
  scale: number,
): void {
  const pts = points.filter((p, i) => {
    const prev = points[i - 1];
    return !prev || Math.hypot(p.x - prev.x, p.y - prev.y) >= 0.01;
  });
  if (pts.length < 2) return;
  const radius = TRACK_BEND_RADIUS * scale;
  const bend = (prev: PathPoint, at: PathPoint, next: PathPoint): void => {
    const a = Math.hypot(at.x - prev.x, at.y - prev.y);
    const b = Math.hypot(next.x - at.x, next.y - at.y);
    const cos = ((at.x - prev.x) * (next.x - at.x) + (at.y - prev.y) * (next.y - at.y)) / (a * b || 1);
    if (cos > 0.999 || cos < -0.999) {
      ctx.lineTo(at.x, at.y);
      return;
    }
    const halfTurn = Math.tan(Math.acos(cos) / 2);
    ctx.arcTo(at.x, at.y, next.x, next.y, Math.min(radius, Math.min(a, b) / 2 / halfTurn));
  };
  const n = pts.length;
  if (closed && n > 2) {
    ctx.moveTo((pts[n - 1].x + pts[0].x) / 2, (pts[n - 1].y + pts[0].y) / 2);
    for (let i = 0; i < n; i++) bend(pts[(i + n - 1) % n], pts[i], pts[(i + 1) % n]);
    ctx.closePath();
    return;
  }
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i + 1 < n; i++) bend(pts[i - 1], pts[i], pts[i + 1]);
  ctx.lineTo(pts[n - 1].x, pts[n - 1].y);
}
