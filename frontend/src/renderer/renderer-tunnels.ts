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
import { studBar } from '../lib/track-stud';

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
  /** The currently selected pair, drawn highlighted with its connecting line. */
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
  /** track_bits of the tile at (x,y), so the stud points across the right rail. */
  trackBitsAt(x: number, y: number): number;
  trackHandler: {
    getEdgePoints(half: number): Record<string, EdgePoint>;
  };
}

// Screen-space outward sign per edge (away from the tile centre, toward the open
// side of the dead-end) — used to point the triangle apex off the buffer.
const TUNNEL_OUTWARD: Record<string, { x: number; y: number }> = {
  N: { x: 1, y: -1 },
  E: { x: 1, y: 1 },
  S: { x: -1, y: 1 },
  W: { x: -1, y: -1 },
};

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

    // Connecting lines: every pair behind the debug toggle, otherwise just the
    // selected pair — clicking a mouth is what reveals where it goes, so a
    // crowded map stays free of crossing lines until you ask.
    const links: TunnelPair[] = [];
    if (showLinks) {
      pairs.forEach((pair) => links.push(pair));
    } else if (selected) {
      links.push(selected);
    }
    links.forEach((pair) => this._drawLink(pair.a, pair.b));

    const isEmphasized = (pair: TunnelPair): boolean =>
      selected != null && this._samePair(pair, selected);

    // Assign a stable per-pair label "T1", "T2" by index of the
    // pair record. The backend already collapses the symmetric
    // dict into one record per pair.
    pairs.forEach((pair, idx) => {
      const label = `T${idx + 1}`;
      const emph = isEmphasized(pair);
      this._drawMouth(pair.a, label, emph);
      this._drawMouth(pair.b, label, emph);
      // Both mouths select the same pair.
      this._registerPlacement(pair, pair.a);
      this._registerPlacement(pair, pair.b);
    });

    if (pendingEndpoint) {
      this._drawMouth(pendingEndpoint, '?', false, true);
    }
  }

  /** Cache one tunnel mouth as a circular click target in screen space. */
  _registerPlacement(pair: TunnelPair, ep: TunnelEndpoint): void {
    const pos = this._endpointScreenPos(ep);
    this._placements.push({
      pair,
      x: pos.x,
      y: pos.y,
      // Centered on the stud, sized so the whole bar is an easy click target.
      r: Math.max(14, 14 * this.scale),
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

  /**
   * Like tunnelAt, but also reports which mouth of the pair was hit and which
   * one is its partner — so a caller can reveal/jump to the far end.
   */
  tunnelMouthAt(
    x: number,
    y: number,
  ): { pair: TunnelPair; clicked: TunnelEndpoint; partner: TunnelEndpoint } | null {
    const pair = this.tunnelAt(x, y);
    if (!pair) return null;
    const d2 = (ep: TunnelEndpoint): number => {
      const pos = this._endpointScreenPos(ep);
      return (x - pos.x) ** 2 + (y - pos.y) ** 2;
    };
    const clickedA = d2(pair.a) <= d2(pair.b);
    return {
      pair,
      clicked: clickedA ? pair.a : pair.b,
      partner: clickedA ? pair.b : pair.a,
    };
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

  _drawMouth(ep: TunnelEndpoint, label: string, emphasized: boolean, pending = false): void {
    const p = getCanvasPalette();
    const tileSize = this.gridSize * this.scale;
    const screenX = this.gridToScreenX(ep.x, ep.y);
    const screenY = this.gridToScreenY(ep.x, ep.y);
    const edge = String(ep.edge).toUpperCase();
    const bits = this.renderer.trackBitsAt(ep.x, ep.y);

    // The mouth is a triangle at the dead-end edge: its base sits square across
    // the rail where the dead-end stud's bar would be (studBar), and the apex
    // points outward off the buffer. Accent-filled, brighter and larger when
    // selected; the pair label rides inside it. (A tunneled edge has no plain
    // dead-end stud — the backend drops it from dead_end_ports — so they never
    // overlap.)
    const baseHalf = (emphasized ? 16 : 13) * this.scale;
    const height = (emphasized ? 22 : 18) * this.scale;
    const bar = studBar(edge, bits, screenX, screenY, tileSize, baseHalf);

    // Apex direction: the base bar rotated 90° (i.e. along the rail), flipped to
    // point away from the tile centre.
    let rx = -(bar.b.y - bar.a.y);
    let ry = bar.b.x - bar.a.x;
    const rlen = Math.hypot(rx, ry) || 1;
    rx /= rlen;
    ry /= rlen;
    const out = TUNNEL_OUTWARD[edge] || { x: 0, y: -1 };
    if (rx * out.x + ry * out.y < 0) {
      rx = -rx;
      ry = -ry;
    }
    const apexX = bar.center.x + rx * height;
    const apexY = bar.center.y + ry * height;

    this.ctx.save();
    this.ctx.beginPath();
    this.ctx.moveTo(bar.a.x, bar.a.y);
    this.ctx.lineTo(bar.b.x, bar.b.y);
    this.ctx.lineTo(apexX, apexY);
    this.ctx.closePath();
    this.ctx.fillStyle = pending ? p.caution : emphasized ? p.accentBright : p.accent;
    this.ctx.fill();
    this.ctx.strokeStyle = withAlpha(p.ink, 0.85);
    this.ctx.lineWidth = Math.max(1, this.scale);
    this.ctx.stroke();

    // Label inside, at the triangle's centroid.
    this.ctx.fillStyle = pending ? p.ink : p.text;
    this.ctx.font = `${Math.max(8, 8 * this.scale)}px Inter, sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(label, (bar.a.x + bar.b.x + apexX) / 3, (bar.a.y + bar.b.y + apexY) / 3);
    this.ctx.restore();
  }
}
