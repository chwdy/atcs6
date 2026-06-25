// renderer-tunnels.ts — draws tunnel mouths and (debug) pairing links.
//
// Migrated from static/js/renderer-tunnels.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-tunnels.test.ts.
//
// A canvas-drawing handler owned by the Renderer: renderer.js does
// `new RendererTunnelHandler(this)` and calls drawTunnels() each world-layer
// flush. It reaches back through its `renderer` reference for the 2D context,
// the grid<->screen projection, and the track handler's edge-point geometry.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererTunnelHandler(this)`).

import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';

export interface EdgePoint {
  x: number;
  y: number;
}

export interface TunnelEndpoint {
  x: number;
  y: number;
  edge: string;
}

export interface TunnelPair {
  a: TunnelEndpoint;
  b: TunnelEndpoint;
}

export interface DrawTunnelsOptions {
  showLinks?: boolean;
  /** The currently selected pair, drawn highlighted. */
  selected?: TunnelPair | null;
}

/** A cached on-screen click target for one tunnel mouth. */
interface TunnelPlacement {
  pair: TunnelPair;
  x: number;
  y: number;
  r: number;
}

/** The slice of the Renderer the tunnel handler reads through its back-reference. */
export interface TunnelHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  trackHandler: {
    getEdgePoints(half: number): Record<string, EdgePoint>;
  };
}

export class RendererTunnelHandler {
  renderer: TunnelHandlerRenderer;
  // Click targets for tunnel mouths, rebuilt every drawTunnels() so a
  // hit-test runs against what is actually on screen. Mirrors the
  // `_placements` cache the station label handler keeps for stationAt().
  _placements: TunnelPlacement[] = [];

  constructor(renderer: TunnelHandlerRenderer) {
    this.renderer = renderer;
  }

  get ctx(): CanvasRenderingContext2D {
    return this.renderer.ctx;
  }
  get scale(): number {
    return this.renderer.scale;
  }
  get gridSize(): number {
    return this.renderer.gridSize;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  drawTunnels(
    tunnels: TunnelPair[] | null | undefined,
    pendingEndpoint?: TunnelEndpoint | null,
    opts: DrawTunnelsOptions = {},
  ): void {
    const p = getCanvasPalette();
    const pairs: TunnelPair[] = Array.isArray(tunnels) ? tunnels : [];
    const { showLinks = false, selected = null } = opts;

    // Rebuild the click-target registry from scratch each draw.
    this._placements = [];

    if (showLinks) {
      pairs.forEach((pair) => this._drawLink(pair.a, pair.b));
    }

    // Assign a stable per-pair label "T1", "T2" by index of the
    // pair record. The backend already collapses the symmetric
    // dict into one record per pair.
    pairs.forEach((pair, idx) => {
      const label = `T${idx + 1}`;
      const isSelected = selected != null && this._samePair(pair, selected);
      const color = isSelected ? p.text : p.accent;
      this._drawMouth(pair.a, label, color, { selected: isSelected });
      this._drawMouth(pair.b, label, color, { selected: isSelected });
      // Both mouths select the same pair.
      this._registerPlacement(pair, pair.a);
      this._registerPlacement(pair, pair.b);
    });

    if (pendingEndpoint) {
      this._drawMouth(pendingEndpoint, '?', p.caution, { pending: true });
    }
  }

  /** Cache one tunnel mouth as a circular click target in screen space. */
  _registerPlacement(pair: TunnelPair, ep: TunnelEndpoint): void {
    const pos = this._endpointScreenPos(ep);
    this._placements.push({
      pair,
      x: pos.x,
      y: pos.y,
      // A touch larger than the 9·scale glyph ring so the mouth is easy to hit.
      r: Math.max(12, 11 * this.scale),
    });
  }

  /**
   * Resolve the tunnel pair whose mouth is under a canvas-relative point, or
   * null. When mouths overlap, the nearest one wins. Used for click-to-select.
   */
  tunnelAt(x: number, y: number): TunnelPair | null {
    let best: TunnelPair | null = null;
    let bestDist = Infinity;
    for (const pl of this._placements) {
      const dx = x - pl.x;
      const dy = y - pl.y;
      const dist = Math.hypot(dx, dy);
      if (dist <= pl.r && dist < bestDist) {
        best = pl.pair;
        bestDist = dist;
      }
    }
    return best;
  }

  /** True when two pair records describe the same tunnel, ignoring A/B order. */
  _samePair(a: TunnelPair, b: TunnelPair): boolean {
    const eq = (p: TunnelEndpoint, q: TunnelEndpoint) =>
      Number(p.x) === Number(q.x) &&
      Number(p.y) === Number(q.y) &&
      String(p.edge).toUpperCase() === String(q.edge).toUpperCase();
    return (eq(a.a, b.a) && eq(a.b, b.b)) || (eq(a.a, b.b) && eq(a.b, b.a));
  }

  _drawLink(epA: TunnelEndpoint, epB: TunnelEndpoint): void {
    const p = getCanvasPalette();
    const aPos = this._endpointScreenPos(epA);
    const bPos = this._endpointScreenPos(epB);
    this.ctx.save();
    this.ctx.strokeStyle = withAlpha(p.accent, 0.75);
    this.ctx.lineWidth = Math.max(1, 1.5 * this.scale);
    this.ctx.setLineDash([6 * this.scale, 4 * this.scale]);
    this.ctx.beginPath();
    this.ctx.moveTo(aPos.x, aPos.y);
    this.ctx.lineTo(bPos.x, bPos.y);
    this.ctx.stroke();
    this.ctx.restore();
  }

  _endpointScreenPos(ep: TunnelEndpoint): EdgePoint {
    const cellSize = this.gridSize * this.scale;
    const half = cellSize / 2;
    const cx = this.gridToScreenX(ep.x, ep.y);
    const cy = this.gridToScreenY(ep.x, ep.y);
    const edge = String(ep.edge).toUpperCase();
    const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
    const edgePoint = edgePoints[edge] || { x: 0, y: 0 };
    return { x: cx + edgePoint.x, y: cy + edgePoint.y };
  }

  _drawMouth(
    ep: TunnelEndpoint,
    label: string,
    color: string,
    opts: { pending?: boolean; selected?: boolean } = {},
  ): void {
    const { pending = false, selected = false } = opts;
    const p = getCanvasPalette();
    const cellSize = this.gridSize * this.scale;
    const half = cellSize / 2;
    const cx = this.gridToScreenX(ep.x, ep.y);
    const cy = this.gridToScreenY(ep.x, ep.y);
    const edge = String(ep.edge).toUpperCase();

    // Push the glyph to the tile's edge midpoint in screen space.
    // Edge offsets here are in the isometric world the renderer uses;
    // each cardinal maps to one of the four tile corners (see
    // getEdgePoints in renderer-tracks.js).
    const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
    const edgePoint = edgePoints[edge] || { x: 0, y: 0 };

    this.ctx.save();
    this.ctx.translate(cx + edgePoint.x, cy + edgePoint.y);

    // Outer ring — a selected mouth gets a larger, brighter, thicker ring.
    this.ctx.beginPath();
    this.ctx.arc(0, 0, (selected ? 11 : 9) * this.scale, 0, Math.PI * 2);
    this.ctx.fillStyle = pending
      ? withAlpha(p.caution, 0.25)
      : selected
        ? withAlpha(p.accent, 0.45)
        : withAlpha(p.accent, 0.18);
    this.ctx.fill();
    this.ctx.strokeStyle = color;
    this.ctx.lineWidth = selected ? Math.max(2.5, 3 * this.scale) : Math.max(1.5, 2 * this.scale);
    this.ctx.stroke();

    // Label.
    this.ctx.fillStyle = p.text;
    this.ctx.font = `${Math.max(9, 10 * this.scale)}px Inter, sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(label, 0, 0);

    this.ctx.restore();
  }
}
