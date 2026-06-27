// renderer-tracks.ts — draws track tiles (straight/diagonal segments, switches
// with their state badge + lock rings, dead-ends, crossings) and the editor
// preview overlays. Owns the shared getEdgePoints() / drawGridSegment() geometry
// that the other renderer handlers reach for through the renderer.
//
// Migrated from static/js/renderer-tracks.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-tracks.test.ts.
//
// Imports getActiveSegments, isCrossing, TRACK_WIDTH from ../lib/constants.
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererTrackHandler(this)`).

import { getActiveSegments, trackBitsToEdges, isCrossing, TRACK_WIDTH, EDGE_MASKS, OPPOSITE_EDGE } from '../lib/constants';
import { getBlockStateColor } from '../lib/helpers';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';
import type { Signal } from './renderer-signals';

export interface EdgePoint {
  x: number;
  y: number;
}

/**
 * Colors for drawTileExtentHighlight — the track-aware overlay used for the
 * box/lasso/paint selection and the block/platform drag-build preview. Both
 * flows share the geometry; only these four colors differ between them.
 */
export interface TileExtentStyle {
  /** Faint diamond fill, so a tile with no track still reads as selected. */
  tileFill: string;
  /** Wide, soft underlay that haloes the track stroke. */
  glow: string;
  /** Bright thin line over the glow — the "this track is selected" mark. */
  core: string;
  /** End-cap tick, drawn across every track edge that leaves the set. */
  cap: string;
}

interface SwitchEntity {
  occupied_by?: unknown;
  train_protection_claims?: unknown[];
  locks?: unknown[];
}

export interface Track {
  x: number;
  y: number;
  track_bits?: number;
  block_section_id?: string | null;
  occupied_by?: unknown;
  reserved_by?: unknown;
  is_switch?: boolean;
  switch_state?: number;
  is_diagonal_only_switch?: boolean;
  switch_points?: Record<string, string[]>;
  switch_id?: string;
  signals?: Signal[];
  dead_end_ports?: string[];
}

/** The slice of the Renderer the track handler reads through its back-reference. */
export interface TrackHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, { state?: string; disabled?: boolean; manual?: boolean } | undefined> | null;
  currentNetwork?: { switches?: Record<string, SwitchEntity | undefined> } | null;
  // "x,y" -> track, used by drawTileExtentHighlight to read each selected
  // tile's track_bits so it can light the real stroke (not just the cell).
  trackMap?: Record<string, { track_bits?: number; block_section_id?: string | null } | undefined>;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  signalHandler: { drawSingleSignal(signal: unknown, trackBits: number | undefined, half: number): void };
  debugHandler: { highlightDeadEnds(x: number, y: number, deadEnds: string[], trackBits?: number): void };
}

/**
 * Track fill color for a block, by availability then live state. Availability
 * (disabled/manual) is a separate axis that overrides live state: a disabled
 * block reads grey and a manual-mode block reads orange regardless of
 * occupancy/reservation. Disabled wins over manual, matching
 * deriveAvailabilityState; the grey/orange match the --block-disabled /
 * --block-manual design tokens. Returns null when the block is absent or its
 * state is unrecognized, so the caller keeps its default track color.
 */
export function blockTrackColor(
  block: { state?: string; disabled?: boolean; manual?: boolean } | undefined | null,
): string | null {
  if (!block) return null;
  // Consume the --block-* design tokens via the canonical resolver, the same
  // path renderer-blocks / train-monitor use (DESIGN_LANGUAGE.md: "Track / block
  // renderers consume these tokens; do not hard-code hex literals at the call
  // site"). Availability (disabled/manual) overrides live state.
  if (block.disabled) return getBlockStateColor('disabled');
  if (block.manual) return getBlockStateColor('manual');
  const state = (block.state || 'free').toLowerCase();
  if (state === 'occupied' || state === 'reserved' || state === 'free' || state === 'caution') {
    return getBlockStateColor(state);
  }
  return null;
}

/**
 * Darken a resolved CSS color (hex `#rrggbb` or `rgb()/rgba()`) toward black by
 * `factor` (0 = black, 1 = unchanged). Used to push the unselected switch branch
 * a step darker than the already-dim `trackDim` token, theme-awarely.
 */
export function darkenColor(color: string, factor: number): string {
  let r: number;
  let g: number;
  let b: number;
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color.trim());
  if (hex) {
    r = parseInt(hex[1], 16);
    g = parseInt(hex[2], 16);
    b = parseInt(hex[3], 16);
  } else {
    const rgb = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(color);
    if (!rgb) return color;
    r = +rgb[1];
    g = +rgb[2];
    b = +rgb[3];
  }
  return `rgb(${Math.round(r * factor)}, ${Math.round(g * factor)}, ${Math.round(b * factor)})`;
}

export class RendererTrackHandler {
  renderer: TrackHandlerRenderer;

  constructor(renderer: TrackHandlerRenderer) {
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
  get currentBlocks(): Record<string, { state?: string; disabled?: boolean; manual?: boolean } | undefined> | null | undefined {
    return this.renderer.currentBlocks;
  }
  get currentNetwork(): { switches?: Record<string, SwitchEntity | undefined> } | null | undefined {
    return this.renderer.currentNetwork;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  drawTrack(track: Track, drawSignals = true): void {
    const p = getCanvasPalette();
    const cellSize = this.gridSize * this.scale;
    const cx = this.gridToScreenX(track.x, track.y);
    const cy = this.gridToScreenY(track.x, track.y);
    const half = cellSize / 2;

    const trackBits = track.track_bits ?? 0;

    this.ctx.save();
    this.ctx.translate(cx, cy);

    // Clip everything this tile draws to the tile's own diamond (its four corner
    // coordinates). A perpendicular-capped band reaches the edge points exactly,
    // but at a diagonal segment its cap corners would still poke past the border
    // into the neighbour tile — which is a different block, so its color would
    // bleed in. The clip keeps each tile's color strictly inside its own
    // diamond; the neighbour draws its own half of any rail that sits on the
    // shared border, so the rail still reads continuous across the seam.
    this.ctx.beginPath();
    this.ctx.moveTo(0, -half);
    this.ctx.lineTo(half, 0);
    this.ctx.lineTo(0, half);
    this.ctx.lineTo(-half, 0);
    this.ctx.closePath();
    this.ctx.clip();

    let trackColor = p.track;
    if (track.block_section_id && this.currentBlocks) {
      const resolved = blockTrackColor(this.currentBlocks[track.block_section_id]);
      if (resolved) trackColor = resolved;
    } else if (track.occupied_by) {
      trackColor = p.danger;
    } else if (track.reserved_by) {
      trackColor = p.caution;
    }

    const isSwitch = track.is_switch || false;
    const switchState = track.switch_state || 0;
    const isDiagonalOnly = track.is_diagonal_only_switch || false;
    const switchPoints = track.switch_points || {};

    // For each edge that opens onto a SAME-block neighbour whose rail meets it,
    // record the neighbour's outgoing direction (the way its rail leaves the
    // shared border). drawTrackFromBits uses this to MITER the colored rail
    // across the seam: a straight run stays seamless, and a bend turns a clean
    // solid corner — the neighbour mitres the matching half. Every other edge (a
    // different block, or empty) gets a square cap, so one block's color never
    // merges into the next.
    const connectDirs = new Map<string, { x: number; y: number }>();
    const myBlock = track.block_section_id;
    const tileMap = this.renderer.trackMap;
    if (myBlock != null && tileMap) {
      const NEIGHBOR_OFFSET: Record<string, [number, number]> = { N: [0, 1], E: [1, 0], S: [0, -1], W: [-1, 0] };
      const ep = this.getEdgePoints(half);
      for (const edge of ['N', 'E', 'S', 'W']) {
        if (!(trackBits & EDGE_MASKS[edge])) continue; // no rail leaves this edge
        const [dx, dy] = NEIGHBOR_OFFSET[edge];
        const nb = tileMap[`${track.x + dx},${track.y + dy}`];
        if (!nb || nb.block_section_id !== myBlock) continue;
        const oppE = OPPOSITE_EDGE[edge];
        const nbBits = nb.track_bits ?? 0;
        if (!(nbBits & EDGE_MASKS[oppE])) continue; // neighbour rail doesn't meet this border
        let nbOther: string | null = null;
        for (const [s1, s2] of getActiveSegments(nbBits)) {
          if (s1 === oppE) { nbOther = s2; break; }
          if (s2 === oppE) { nbOther = s1; break; }
        }
        if (!nbOther) continue;
        const from = ep[oppE];
        const to = ep[nbOther];
        const vx = to.x - from.x;
        const vy = to.y - from.y;
        const vl = Math.hypot(vx, vy) || 1;
        connectDirs.set(edge, { x: vx / vl, y: vy / vl });
      }
    }

    // Grey trapezoid track bed (the rail clipped to this tile's diamond). It
    // tiles seamlessly with neighbours, so the track itself always reads
    // continuous; the block-status color is then laid on top as a narrower
    // rectangle inside this trapezoid.
    this.drawTrackBed(trackBits, half);

    if (isSwitch) {
      this.drawTrackWithSwitches(trackBits, half, trackColor, switchState, isDiagonalOnly, switchPoints);

      // Two distinct lock states per design/120-switch.md:32 — path-owned
      // future authority (Switch.locks) vs train-owned physical hold
      // (Switch.occupied_by / train_protection_claims) after head-entry
      // consumes the primary lock. Train-owned wins when both present.
      const switchEntity = (track.switch_id && this.currentNetwork && this.currentNetwork.switches)
        ? this.currentNetwork.switches[track.switch_id]
        : null;
      const trainOwned = !!(switchEntity && (
        switchEntity.occupied_by ||
        (Array.isArray(switchEntity.train_protection_claims) && switchEntity.train_protection_claims.length > 0)
      ));

      if (trainOwned) {
        this.ctx.strokeStyle = p.hold; // Brown — train-owned hold
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 6 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();
      } else if (switchEntity && Array.isArray(switchEntity.locks) && switchEntity.locks.length > 0) {
        this.ctx.strokeStyle = p.lock; // Orange — path-owned future lock
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 6 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();
      }
    } else {
      this.drawTrackFromBits(trackBits, half, trackColor, track.dead_end_ports || [], connectDirs);
    }

    if (drawSignals && track.signals && track.signals.length > 0) {
      for (const signal of track.signals) {
        this.renderer.signalHandler.drawSingleSignal(signal, track.track_bits, half);
      }
    }

    this.ctx.restore();

    // NOTE: the switch M/D indicator is NOT drawn here. It's drawn in a separate
    // pass after every tile's rails (renderer _drawWorld -> drawSwitchBadgeAt),
    // so a neighbouring tile's rail or bed can't paint over it, and it's
    // unclipped so it can sit flush against the entry edge.

    if (track.dead_end_ports && track.dead_end_ports.length > 0) {
      this.renderer.debugHandler.highlightDeadEnds(track.x, track.y, track.dead_end_ports, track.track_bits || 0);
    }
  }

  getEdgePoints(half: number): Record<string, EdgePoint> {
    const h = half;
    return {
      N: { x: h / 2, y: -h / 2 },
      E: { x: h / 2, y: h / 2 },
      S: { x: -h / 2, y: h / 2 },
      W: { x: -h / 2, y: -h / 2 },
    };
  }

  drawGridSegment(start: EdgePoint, end: EdgePoint, width: number, e1: string, e2: string, fillColor: string): void {
    const halfWidth = width / 2;

    const getEdgeOffset = (edge: string): EdgePoint => {
      switch (edge) {
        case 'N': return { x: -halfWidth, y: -halfWidth };
        case 'E': return { x: halfWidth, y: -halfWidth };
        case 'S': return { x: halfWidth, y: halfWidth };
        case 'W': return { x: -halfWidth, y: halfWidth };
        default: return { x: 0, y: 0 };
      }
    };

    const offset1 = getEdgeOffset(e1);
    const offset2 = getEdgeOffset(e2);

    const p1 = { x: start.x + offset1.x, y: start.y + offset1.y };
    const p2 = { x: start.x - offset1.x, y: start.y - offset1.y };
    const p3 = { x: end.x + offset2.x, y: end.y + offset2.y };
    const p4 = { x: end.x - offset2.x, y: end.y - offset2.y };

    this.ctx.fillStyle = fillColor;
    this.ctx.beginPath();
    this.ctx.moveTo(p1.x, p1.y);
    this.ctx.lineTo(p2.x, p2.y);
    this.ctx.lineTo(p3.x, p3.y);
    this.ctx.lineTo(p4.x, p4.y);
    this.ctx.closePath();
    this.ctx.fill();
  }

  /**
   * Draw a rail segment as a constant-width rectangle centered on the line from
   * `start` to `end`, with its end caps PERPENDICULAR to that line (the true
   * normal) — not parallel to the tile border the way drawGridSegment's fixed
   * diagonal offset cuts them.
   *
   * Two properties the rail rendering relies on:
   *  - Every segment is the same `width` (a diagonal "turn" is as thick as a
   *    straight) and the caps are square to the path: the rectangle the
   *    block-status color wants.
   *  - The caps land exactly on the tile's edge points, so the color stops at
   *    the tile border instead of bulging past it into the neighbour tile the
   *    way a round cap would (which bleeds one block's color into the next).
   * The grey bed (drawTrackBed) fills the small wedge two rectangles leave where
   * the path bends.
   */
  drawCenterlineBand(start: EdgePoint, end: EdgePoint, width: number, fillColor: string): void {
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const len = Math.hypot(dx, dy);
    if (len === 0) return;
    const halfWidth = width / 2;
    const ox = (-dy / len) * halfWidth;
    const oy = (dx / len) * halfWidth;

    this.ctx.fillStyle = fillColor;
    this.ctx.beginPath();
    this.ctx.moveTo(start.x + ox, start.y + oy);
    this.ctx.lineTo(start.x - ox, start.y - oy);
    this.ctx.lineTo(end.x - ox, end.y - oy);
    this.ctx.lineTo(end.x + ox, end.y + oy);
    this.ctx.closePath();
    this.ctx.fill();
  }

  /**
   * Grey trapezoid track bed for one tile, drawn under drawTrack's diamond clip
   * (it does NOT translate/clip itself — the caller has). Each segment is drawn
   * as a band EXTENDED past its edge points, so the clip trims it to the tile
   * diamond: a trapezoid whose slanted ends ARE the tile borders. Neighbouring
   * tiles' trapezoids meet along those borders, so the grey track reads
   * continuous. The block-status color is laid on top (drawTrackFromBits) as a
   * narrower rectangle inside this trapezoid.
   */
  drawTrackBed(trackBits: number, half: number): void {
    const segments = getActiveSegments(trackBits);
    if (segments.length === 0) return;
    const p = getCanvasPalette();
    const edgePoints = this.getEdgePoints(half);
    // A segment's footprint along a tile border is width / sin(angle it crosses).
    // An axial segment (N-S, E-W) crosses head-on (footprint = width); a diagonal
    // (N-E etc.) crosses at 45° (footprint = width·√2, i.e. √2 wider). At the same
    // perpendicular width the diagonal's grey would spike past the axial's where
    // they meet, so widen the axial grey by √2 — then every segment's border
    // footprint is the same (TRACK_WIDTH·2) and the trapezoids tile with no spike.
    const obliqueWidth = TRACK_WIDTH * Math.SQRT2 * this.scale;
    const axialWidth = TRACK_WIDTH * 2 * this.scale;
    for (const [e1, e2] of segments) {
      const a = edgePoints[e1];
      const b = edgePoints[e2];
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = (dx / len) * half;
      const uy = (dy / len) * half;
      const bedWidth = OPPOSITE_EDGE[e1] === e2 ? axialWidth : obliqueWidth;
      this.drawCenterlineBand({ x: a.x - ux, y: a.y - uy }, { x: b.x + ux, y: b.y + uy }, bedWidth, p.track);
    }
  }

  drawTrackFromBits(
    trackBits: number,
    half: number,
    color: string,
    deadEndPorts: string[] = [],
    connectDirs: Map<string, { x: number; y: number }> = new Map(),
  ): void {
    const p = getCanvasPalette();
    const edgePoints = this.getEdgePoints(half);

    const segments = getActiveSegments(trackBits);
    const railWidth = TRACK_WIDTH * this.scale;

    // Lay the block-status color on top of the grey bed, clipped (by drawTrack)
    // to the tile diamond so it can never bleed into a neighbour. Each segment is
    // a stroked, miter-joined polyline of uniform width:
    //   - A same-block edge (in connectDirs) runs a short STUB in the neighbour's
    //     outgoing direction, through the edge point, into the segment. The miter
    //     join makes a straight run seamless and a bend a clean solid corner; the
    //     neighbour mitres the matching half, and the stub is clipped off.
    //   - Every other edge gets a square (butt) cap pulled `capInset` inside, so
    //     it shows a clean square end (no clip chamfer); the grey trapezoid fills
    //     out to the tile border.
    const capInset = railWidth;
    const stubLen = half; // only sets the miter direction; the stub is clipped off
    this.ctx.strokeStyle = color;
    this.ctx.lineWidth = railWidth;
    this.ctx.lineJoin = 'miter';
    this.ctx.lineCap = 'butt';
    this.ctx.miterLimit = 8;
    for (const [e1, e2] of segments) {
      const a = edgePoints[e1];
      const b = edgePoints[e2];
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const d1 = connectDirs.get(e1);
      const d2 = connectDirs.get(e2);
      const pts: Array<{ x: number; y: number }> = [];
      if (d1) {
        pts.push({ x: a.x + d1.x * stubLen, y: a.y + d1.y * stubLen });
        pts.push(a);
      } else {
        pts.push({ x: a.x + ux * capInset, y: a.y + uy * capInset });
      }
      if (d2) {
        pts.push(b);
        pts.push({ x: b.x + d2.x * stubLen, y: b.y + d2.y * stubLen });
      } else {
        pts.push({ x: b.x - ux * capInset, y: b.y - uy * capInset });
      }
      this.ctx.beginPath();
      this.ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) this.ctx.lineTo(pts[i].x, pts[i].y);
      this.ctx.stroke();
    }

    for (const port of deadEndPorts) {
      const deadEndEdge = port.charAt(0).toUpperCase();

      for (const [e1, e2] of segments) {
        if (e1 === deadEndEdge || e2 === deadEndEdge) {
          const otherEdge = (e1 === deadEndEdge) ? e2 : e1;
          const deadEndPoint = edgePoints[deadEndEdge];
          const otherPoint = edgePoints[otherEdge];

          const isAxial = (deadEndEdge === 'N' && otherEdge === 'S') ||
            (deadEndEdge === 'S' && otherEdge === 'N') ||
            (deadEndEdge === 'E' && otherEdge === 'W') ||
            (deadEndEdge === 'W' && otherEdge === 'E');

          if (isAxial) {
            this.drawGridSegment({ x: 0, y: 0 }, otherPoint, TRACK_WIDTH * this.scale, deadEndEdge, otherEdge, color);
          } else {
            this.drawGridSegment(deadEndPoint, otherPoint, TRACK_WIDTH * this.scale, e1, e2, color);
          }
          break;
        }
      }
    }

    if (isCrossing(trackBits)) {
      this.ctx.fillStyle = p.accent;
      this.ctx.beginPath();
      this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
      this.ctx.fill();
    }
  }

  drawTrackWithSwitches(
    trackBits: number,
    half: number,
    color: string,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): void {
    const p = getCanvasPalette();
    const edgePoints = this.getEdgePoints(half);

    const SEGMENTS = [
      { bit: 0, edges: ['N', 'S'], name: 'NS' },
      { bit: 1, edges: ['E', 'W'], name: 'EW' },
      { bit: 2, edges: ['N', 'E'], name: 'NE' },
      { bit: 3, edges: ['N', 'W'], name: 'NW' },
      { bit: 4, edges: ['S', 'E'], name: 'SE' },
      { bit: 5, edges: ['S', 'W'], name: 'SW' },
    ];

    const activeSegmentBits = new Set<number>();

    if (isDiagonalOnly && switchPoints) {
      const CLOCKWISE = ['N', 'E', 'S', 'W'];

      for (const [entryEdge, exits] of Object.entries(switchPoints)) {
        if (!exits || exits.length < 2) continue;

        const sortedExits = [...exits].sort((a, b) =>
          CLOCKWISE.indexOf(a) - CLOCKWISE.indexOf(b),
        );

        const activeExit = switchState === 0 ? sortedExits[0] : sortedExits[1];

        for (const seg of SEGMENTS) {
          if (!(trackBits & (1 << seg.bit))) continue;
          const [e1, e2] = seg.edges;
          if ((e1 === entryEdge && e2 === activeExit) ||
            (e2 === entryEdge && e1 === activeExit)) {
            activeSegmentBits.add(seg.bit);
          }
        }
      }
    }

    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;

      const [e1, e2] = seg.edges;
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (!start || !end) continue;

      let isActive;
      if (isDiagonalOnly) {
        isActive = activeSegmentBits.has(seg.bit);
      } else {
        const isLongSegment = seg.bit <= 1;
        isActive = (isLongSegment && switchState === 0) ||
          (!isLongSegment && switchState === 1);
      }

      if (!isActive) {
        // The unselected branch: a wide, very dark arm drawn with the SAME
        // geometry as the grey bed — extended past the edge points (the tile clip
        // trims it to the diamond) and footprint-matched (axial √2 wider). It
        // covers the bed for this branch, so it reads as one solid dark arm with
        // no spike where the diverge route meets an unconnected tile.
        const segmentColor = darkenColor(p.trackDim, 0.5);
        const isAxial = OPPOSITE_EDGE[e1] === e2;
        const segmentWidth = (isAxial ? TRACK_WIDTH * 2 : TRACK_WIDTH * Math.SQRT2) * this.scale;
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = (dx / len) * half;
        const uy = (dy / len) * half;
        this.drawCenterlineBand(
          { x: start.x - ux, y: start.y - uy },
          { x: end.x + ux, y: end.y + uy },
          segmentWidth,
          segmentColor,
        );
      }
    }

    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;

      const [e1, e2] = seg.edges;
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (!start || !end) continue;

      let isActive;
      if (isDiagonalOnly) {
        isActive = activeSegmentBits.has(seg.bit);
      } else {
        const isLongSegment = seg.bit <= 1;
        isActive = (isLongSegment && switchState === 0) ||
          (!isLongSegment && switchState === 1);
      }

      if (isActive) {
        const segmentColor = color;
        const segmentWidth = TRACK_WIDTH * this.scale;
        this.drawCenterlineBand(start, end, segmentWidth, segmentColor);
      }
    }

    // The M/D indicator is drawn separately (drawSwitchBadge), UNCLIPPED, so it
    // can stick right to the entry edge without the tile clip cutting it.
  }

  /**
   * Draw the switch's M/D indicator at the root of its two branches — right at
   * the entry edge (the switch_points key). The caller has translated to the
   * tile centre and must NOT have the tile clip active, so the badge can sit
   * flush against the edge instead of being trimmed by the diamond.
   */
  drawSwitchBadge(
    half: number,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): void {
    const p = getCanvasPalette();
    const edgePoints = this.getEdgePoints(half);
    const entryEdge = Object.keys(switchPoints)[0];
    const entryPt = entryEdge ? edgePoints[entryEdge] : null;
    const f = 0.9; // stick to the entry edge (root of the branches)
    const bx = entryPt ? entryPt.x * f : 0;
    const by = entryPt ? entryPt.y * f : 0;

    const isMain = switchState === 0;
    this.ctx.fillStyle = isMain ? p.success : p.caution;
    this.ctx.beginPath();
    this.ctx.arc(bx, by, 5 * this.scale, 0, Math.PI * 2);
    this.ctx.fill();

    this.ctx.fillStyle = p.onAccent;
    this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(isMain ? 'M' : 'D', bx, by);

    if (isDiagonalOnly) {
      this.ctx.font = `${7 * this.scale}px sans-serif`;
      this.ctx.fillStyle = p.warning;
      this.ctx.fillText('⟳', bx + 9 * this.scale, by - 7 * this.scale);
    }
  }

  /**
   * Draw one switch tile's M/D indicator, translated to the tile centre and
   * unclipped. The renderer calls this in a pass AFTER every tile's rails so a
   * neighbouring tile can't paint over the badge.
   */
  drawSwitchBadgeAt(track: Track): void {
    if (!track.is_switch) return;
    const half = (this.gridSize * this.scale) / 2;
    const cx = this.gridToScreenX(track.x, track.y);
    const cy = this.gridToScreenY(track.x, track.y);
    this.ctx.save();
    this.ctx.translate(cx, cy);
    this.drawSwitchBadge(
      half,
      track.switch_state || 0,
      track.is_diagonal_only_switch || false,
      track.switch_points || {},
    );
    this.ctx.restore();
  }

  drawPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;

    const p = getCanvasPalette();
    this.ctx.save();

    this.ctx.strokeStyle = withAlpha(p.accent, 0.6);
    this.ctx.fillStyle = withAlpha(p.accent, 0.3);
    this.ctx.lineWidth = 3 * this.scale;
    this.ctx.setLineDash([5, 5]);

    const cellSize = this.gridSize * this.scale;

    for (let i = 0; i < path.length; i++) {
      const pos = path[i];
      const cx = this.gridToScreenX(pos.x, pos.y);
      const cy = this.gridToScreenY(pos.x, pos.y);

      this.ctx.beginPath();
      this.ctx.arc(cx, cy, cellSize / 3, 0, Math.PI * 2);
      this.ctx.fill();

      if (i < path.length - 1) {
        const next = path[i + 1];
        const nx = this.gridToScreenX(next.x, next.y);
        const ny = this.gridToScreenY(next.x, next.y);

        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(nx, ny);
        this.ctx.stroke();
      }
    }

    this.ctx.restore();
  }

  // Drawn when a track drag has no clean route: a red dashed line from the
  // start tile to the cursor, so the operator sees the tool is tracking but
  // cannot build here (e.g. a platform in the way, or a forced segment that
  // can't turn without a square corner). Nothing is committed in this state.
  drawInvalidPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;

    const p = getCanvasPalette();
    this.ctx.save();

    this.ctx.strokeStyle = withAlpha(p.danger, 0.85);
    this.ctx.fillStyle = withAlpha(p.danger, 0.35);
    this.ctx.lineWidth = 3 * this.scale;
    this.ctx.setLineDash([6, 4]);

    const cellSize = this.gridSize * this.scale;

    for (let i = 0; i < path.length; i++) {
      const pos = path[i];
      const cx = this.gridToScreenX(pos.x, pos.y);
      const cy = this.gridToScreenY(pos.x, pos.y);

      this.ctx.beginPath();
      this.ctx.arc(cx, cy, cellSize / 4, 0, Math.PI * 2);
      this.ctx.fill();

      if (i < path.length - 1) {
        const next = path[i + 1];
        const nx = this.gridToScreenX(next.x, next.y);
        const ny = this.gridToScreenY(next.x, next.y);

        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(nx, ny);
        this.ctx.stroke();
      }
    }

    this.ctx.restore();
  }

  drawPreviewTracks(tracks: Track[] | null | undefined): void {
    if (!tracks || tracks.length === 0) {
      return;
    }

    const p = getCanvasPalette();
    const cellSize = this.gridSize * this.scale;
    const half = cellSize / 2;
    const previewColor = withAlpha(p.caution, 0.8);

    for (const track of tracks) {
      const trackBits = track.track_bits ?? 0;
      if (trackBits === 0) continue;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);

      this.ctx.save();
      this.ctx.translate(cx, cy);

      const edgePoints = this.getEdgePoints(half);
      this.ctx.strokeStyle = previewColor;
      this.ctx.lineWidth = 5 * this.scale;
      this.ctx.lineCap = 'square';

      const segments = getActiveSegments(trackBits);
      for (const [e1, e2] of segments) {
        const start = edgePoints[e1];
        const end = edgePoints[e2];
        if (start && end) {
          this.ctx.beginPath();
          this.ctx.moveTo(start.x, start.y);
          this.ctx.lineTo(end.x, end.y);
          this.ctx.stroke();
        }
      }

      if (isCrossing(trackBits)) {
        this.ctx.fillStyle = withAlpha(p.caution, 0.9);
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
        this.ctx.fill();
      }

      this.ctx.restore();
    }
  }

  drawHoverPreview(
    gridPos: { x: number; y: number },
    toolType: string,
    isValid: boolean,
    signalDirection: string | null = null,
    trackBits: number | null = null,
  ): void {
    const p = getCanvasPalette();
    const cx = this.gridToScreenX(gridPos.x, gridPos.y);
    const cy = this.gridToScreenY(gridPos.x, gridPos.y);
    const half = (this.gridSize * this.scale) / 2;

    this.ctx.save();

    this.ctx.strokeStyle = isValid ? withAlpha(p.success, 0.8) : withAlpha(p.danger, 0.5);
    this.ctx.fillStyle = isValid ? withAlpha(p.success, 0.2) : withAlpha(p.danger, 0.1);
    this.ctx.lineWidth = 2 * this.scale;

    this.ctx.beginPath();
    this.ctx.moveTo(cx, cy - half);
    this.ctx.lineTo(cx + half, cy);
    this.ctx.lineTo(cx, cy + half);
    this.ctx.lineTo(cx - half, cy);
    this.ctx.closePath();
    this.ctx.fill();
    this.ctx.stroke();

    if (isValid) {
      if (toolType === 'signal' && signalDirection) {
        this.ctx.translate(cx, cy);

        const detectionDir = signalDirection.charAt(0).toUpperCase();
        const travelEdge = detectionDir;
        const edgePoints = this.getEdgePoints(half);
        const isLongPath = (trackBits === 1 || trackBits === 2);
        const isDiagonal = (trackBits === 4 || trackBits === 8 || trackBits === 16 || trackBits === 32);

        let sx, sy;
        if (isLongPath) {
          const rightSide = ({
            N: 'E', S: 'W', E: 'S', W: 'N',
          } as Record<string, string>)[travelEdge] || 'E';
          const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
          sx = sidePoint.x * 0.5;
          sy = sidePoint.y * 0.5;
        } else {
          sx = 0;
          sy = 0;
        }

        let dirX, dirY;
        if (isDiagonal) {
          let diagonalX, diagonalY;
          switch (trackBits) {
            case 4: diagonalX = 0; diagonalY = -1; break;
            case 8: diagonalX = 1; diagonalY = 0; break;
            case 16: diagonalX = -1; diagonalY = 0; break;
            case 32: diagonalX = 0; diagonalY = 1; break;
            default: diagonalX = 1; diagonalY = 0; break;
          }

          let edge1, edge2;
          switch (trackBits) {
            case 4: edge1 = 'N'; edge2 = 'E'; break;
            case 8: edge1 = 'N'; edge2 = 'W'; break;
            case 16: edge1 = 'S'; edge2 = 'E'; break;
            case 32: edge1 = 'S'; edge2 = 'W'; break;
            default: edge1 = 'N'; edge2 = 'E'; break;
          }

          if (detectionDir === edge1) {
            diagonalX = -diagonalX;
            diagonalY = -diagonalY;
          }

          const len = Math.sqrt(diagonalX * diagonalX + diagonalY * diagonalY);
          dirX = diagonalX / len;
          dirY = diagonalY / len;
        } else {
          const approachEdge = ({ N: 'S', S: 'N', E: 'W', W: 'E' } as Record<string, string>)[detectionDir] || 'W';
          const dirPoint = edgePoints[approachEdge] || { x: 1, y: 0 };
          const len = Math.sqrt(dirPoint.x * dirPoint.x + dirPoint.y * dirPoint.y);
          dirX = dirPoint.x / len;
          dirY = dirPoint.y / len;
        }

        this.ctx.strokeStyle = withAlpha(p.textDim, 0.8);
        this.ctx.lineWidth = 1.5 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, 4 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();

        const stickLength = half * 0.4;
        const stickEndX = sx + dirX * stickLength;
        const stickEndY = sy + dirY * stickLength;

        this.ctx.strokeStyle = withAlpha(p.textDim, 0.8);
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.moveTo(sx, sy);
        this.ctx.lineTo(stickEndX, stickEndY);
        this.ctx.stroke();

        const innerRadius = 3 * this.scale;
        this.ctx.fillStyle = withAlpha(p.success, 0.7);
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, innerRadius, 0, Math.PI * 2);
        this.ctx.fill();

        const orthoX = -dirY;
        const orthoY = dirX;
        const orthoLen = half * 0.15;

        this.ctx.beginPath();
        this.ctx.moveTo(stickEndX - orthoX * orthoLen, stickEndY - orthoY * orthoLen);
        this.ctx.lineTo(stickEndX + orthoX * orthoLen, stickEndY + orthoY * orthoLen);
        this.ctx.stroke();

        this.ctx.fillStyle = withAlpha(p.text, 0.9);
        this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        const labelX = sx - dirX * half * 0.3;
        const labelY = sy - dirY * half * 0.3;
        this.ctx.fillText(signalDirection.charAt(0), labelX, labelY);
      } else {
        this.ctx.font = `${16 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillStyle = withAlpha(p.text, 0.8);
        const icon = toolType === 'signal' ? '🚦' : '🚉';
        this.ctx.fillText(icon, cx, cy);
      }
    }

    this.ctx.restore();
  }

  drawEraserPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;

    const p = getCanvasPalette();
    this.ctx.save();
    this.ctx.strokeStyle = withAlpha(p.danger, 0.8);
    this.ctx.fillStyle = withAlpha(p.danger, 0.3);
    this.ctx.lineWidth = 4 * this.scale;
    this.ctx.setLineDash([6, 4]);

    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;

    for (const pos of path) {
      const cx = this.gridToScreenX(pos.x, pos.y);
      const cy = this.gridToScreenY(pos.x, pos.y);

      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.fill();

      const xSize = halfTile * 0.4;
      this.ctx.beginPath();
      this.ctx.moveTo(cx - xSize, cy - xSize);
      this.ctx.lineTo(cx + xSize, cy + xSize);
      this.ctx.moveTo(cx + xSize, cy - xSize);
      this.ctx.lineTo(cx - xSize, cy + xSize);
      this.ctx.stroke();
    }

    this.ctx.setLineDash([]);
    this.ctx.strokeStyle = withAlpha(p.danger, 0.6);
    this.ctx.lineWidth = 2 * this.scale;
    this.ctx.beginPath();
    for (let i = 0; i < path.length; i++) {
      const cx = this.gridToScreenX(path[i].x, path[i].y);
      const cy = this.gridToScreenY(path[i].x, path[i].y);
      if (i === 0) this.ctx.moveTo(cx, cy);
      else this.ctx.lineTo(cx, cy);
    }
    this.ctx.stroke();

    this.ctx.restore();
  }

  /**
   * Track-aware overlay for a set of grid tiles (the box/lasso/paint selection,
   * and the block/platform drag preview). Filling each tile diamond makes a run
   * of diagonal tiles read as a zigzag of cells even when the track through them
   * is a straight line, so instead this:
   *   1. dims the tile diamond (faint), so a tile that carries no track still
   *      reads as selected;
   *   2. lights the actual track stroke on each tile (soft glow + bright core),
   *      so the selection looks like the line of track it really is; and
   *   3. ticks every track edge that exits the set, so the ends of the run —
   *      "where it stops" — are obvious, including a one-tile gap mid-run.
   *
   * `keys` are "x,y" strings; track_bits come from the renderer's trackMap.
   */
  drawTileExtentHighlight(keys: Set<string>, style: TileExtentStyle): void {
    if (!keys || keys.size === 0) return;

    const cellSize = this.gridSize * this.scale;
    const half = cellSize / 2;
    const trackMap = this.renderer.trackMap || {};
    const edgePoints = this.getEdgePoints(half);
    const neighborDeltas: Record<string, { dx: number; dy: number }> = {
      N: { dx: 0, dy: 1 }, E: { dx: 1, dy: 0 }, S: { dx: 0, dy: -1 }, W: { dx: -1, dy: 0 },
    };

    this.ctx.save();

    // 1) Dim tile diamonds, but only for selected tiles that carry NO track:
    //    a tile with track is shown by its rail below (drawing the cell too just
    //    rebuilds the "field of diamonds"), while a selected-but-trackless tile —
    //    e.g. a gap at the end of a run — still shows up as a diamond so it is
    //    easy to spot a missing segment.
    this.ctx.fillStyle = style.tileFill;
    for (const key of keys) {
      if (trackMap[key]?.track_bits) continue;
      const [xs, ys] = key.split(',');
      const cx = this.gridToScreenX(Number(xs), Number(ys));
      const cy = this.gridToScreenY(Number(xs), Number(ys));
      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - half);
      this.ctx.lineTo(cx + half, cy);
      this.ctx.lineTo(cx, cy + half);
      this.ctx.lineTo(cx - half, cy);
      this.ctx.closePath();
      this.ctx.fill();
    }

    // 2) Track stroke: a soft glow pass, then a bright core pass on top.
    this.ctx.lineCap = 'round';
    this.ctx.lineJoin = 'round';
    const passes: Array<{ color: string; width: number }> = [
      { color: style.glow, width: TRACK_WIDTH * 2.1 * this.scale },
      { color: style.core, width: TRACK_WIDTH * 1.2 * this.scale },
    ];
    for (const pass of passes) {
      this.ctx.strokeStyle = pass.color;
      this.ctx.lineWidth = pass.width;
      for (const key of keys) {
        const bits = trackMap[key]?.track_bits ?? 0;
        if (!bits) continue;
        const [xs, ys] = key.split(',');
        const cx = this.gridToScreenX(Number(xs), Number(ys));
        const cy = this.gridToScreenY(Number(xs), Number(ys));
        this.ctx.save();
        this.ctx.translate(cx, cy);
        for (const [e1, e2] of getActiveSegments(bits)) {
          const s = edgePoints[e1];
          const en = edgePoints[e2];
          if (!s || !en) continue;
          this.ctx.beginPath();
          this.ctx.moveTo(s.x, s.y);
          this.ctx.lineTo(en.x, en.y);
          this.ctx.stroke();
        }
        this.ctx.restore();
      }
    }

    // 3) End-caps at every track edge whose neighbor is not in the set: a tick
    //    laid across the rail (perpendicular to the track direction at that
    //    edge) so the run's ends pop.
    this.ctx.strokeStyle = style.cap;
    this.ctx.lineWidth = 2.5 * this.scale;
    const capHalf = half * 0.38;
    for (const key of keys) {
      const bits = trackMap[key]?.track_bits ?? 0;
      if (!bits) continue;
      const [xs, ys] = key.split(',');
      const x = Number(xs);
      const y = Number(ys);
      const segments = getActiveSegments(bits);
      this.ctx.save();
      this.ctx.translate(this.gridToScreenX(x, y), this.gridToScreenY(x, y));
      for (const edge of trackBitsToEdges(bits)) {
        const d = neighborDeltas[edge];
        if (!d || keys.has(`${x + d.dx},${y + d.dy}`)) continue;
        const ep = edgePoints[edge];
        if (!ep) continue;
        // Track direction at this end = edge midpoint minus its segment partner
        // (fall back to the outward radial if the segment has no partner).
        let partner: string | null = null;
        for (const [e1, e2] of segments) {
          if (e1 === edge) { partner = e2; break; }
          if (e2 === edge) { partner = e1; break; }
        }
        const pp = partner ? edgePoints[partner] : null;
        let tx = pp ? ep.x - pp.x : ep.x;
        let ty = pp ? ep.y - pp.y : ep.y;
        const tl = Math.hypot(tx, ty) || 1;
        tx /= tl; ty /= tl;
        const px = -ty;
        const py = tx;
        this.ctx.beginPath();
        this.ctx.moveTo(ep.x - px * capHalf, ep.y - py * capHalf);
        this.ctx.lineTo(ep.x + px * capHalf, ep.y + py * capHalf);
        this.ctx.stroke();
      }
      this.ctx.restore();
    }

    this.ctx.restore();
  }
}
