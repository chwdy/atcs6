// renderer-tracks.ts — draws track tiles (straight/diagonal segments, switches
// with their state badge + lock/occupied dot, dead-ends, crossings) and the editor
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

export interface EdgePoint {
  x: number;
  y: number;
}

/**
 * Colors for drawTileExtentHighlight — the track-aware overlay used for the
 * box/lasso/paint selection and the block drag-build preview. Both
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

export interface Track {
  x: number;
  y: number;
  track_bits?: number;
  block_section_id?: string | null;
  is_switch?: boolean;
  switch_state?: number;
  is_diagonal_only_switch?: boolean;
  switch_points?: Record<string, string[]>;
  switch_id?: string;
  dead_end_ports?: string[];
}

/** The slice of the Renderer the track handler reads through its back-reference. */
interface TrackBlock {
  state?: string;
  disabled?: boolean;
  manual?: boolean;
  reads_occupied?: boolean;
  claimed_by_train_id?: string | null;
}

export interface TrackHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, TrackBlock | undefined> | null;
  currentNetwork?: { operation_mode?: string } | null;
  // "x,y" -> track, used by drawTileExtentHighlight to read each selected
  // tile's track_bits so it can light the real stroke (not just the cell).
  trackMap?: Record<string, { track_bits?: number; block_section_id?: string | null } | undefined>;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
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
  block: TrackBlock | undefined | null,
  detectorColor = false,
): string | null {
  if (!block) return null;
  // Consume the --block-* design tokens via the canonical resolver, the same
  // path renderer-blocks / train-monitor use (DESIGN_LANGUAGE.md: "Track / block
  // renderers consume these tokens; do not hard-code hex literals at the call
  // site"). Availability (disabled/manual) overrides live state.
  if (block.disabled) return getBlockStateColor('disabled');
  if (block.manual) return getBlockStateColor('manual');
  const state = detectorColor && typeof block.reads_occupied === 'boolean'
    ? block.reads_occupied ? 'occupied' : block.claimed_by_train_id ? 'reserved' : 'free'
    : (block.state || 'free').toLowerCase();
  if (state === 'occupied' || state === 'reserved' || state === 'free' || state === 'caution') {
    return getBlockStateColor(state);
  }
  return null;
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
  get currentBlocks(): Record<string, TrackBlock | undefined> | null | undefined {
    return this.renderer.currentBlocks;
  }
  get currentNetwork(): { operation_mode?: string } | null | undefined {
    return this.renderer.currentNetwork;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  drawTrack(track: Track): void {
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
      const mode = this.currentNetwork?.operation_mode;
      const resolved = blockTrackColor(this.currentBlocks[track.block_section_id], !!mode);
      if (resolved) trackColor = resolved;
    }

    const isSwitch = track.is_switch || false;
    const switchState = track.switch_state || 0;
    const isDiagonalOnly = track.is_diagonal_only_switch || false;
    const switchPoints = track.switch_points || {};

    // Record every edge that opens onto a SAME-block neighbour whose rail meets
    // it. drawTrackFromBits EXTENDS the colored rail past these edges so the tile
    // clip trims it flush to the shared border and it tiles seamlessly with the
    // neighbour's half (a straight run stays continuous, a bend turns a clean
    // solid corner, a crossing fills solid). Every other edge (a different block,
    // or empty) stops short with a square end, so one block's color never merges
    // into the next.
    const connectedEdges = new Set<string>();
    const myBlock = track.block_section_id;
    const tileMap = this.renderer.trackMap;
    if (myBlock != null && tileMap) {
      const NEIGHBOR_OFFSET: Record<string, [number, number]> = { N: [0, 1], E: [1, 0], S: [0, -1], W: [-1, 0] };
      for (const edge of ['N', 'E', 'S', 'W']) {
        if (!(trackBits & EDGE_MASKS[edge])) continue; // no rail leaves this edge
        const [dx, dy] = NEIGHBOR_OFFSET[edge];
        const nb = tileMap[`${track.x + dx},${track.y + dy}`];
        if (!nb || nb.block_section_id !== myBlock) continue;
        const oppE = OPPOSITE_EDGE[edge];
        const nbBits = nb.track_bits ?? 0;
        if (!(nbBits & EDGE_MASKS[oppE])) continue; // neighbour rail doesn't meet this border
        connectedEdges.add(edge);
      }
    }

    // Grey trapezoid track bed (the rail clipped to this tile's diamond). It
    // tiles seamlessly with neighbours, so the track always reads continuous;
    // the block-status color is then laid on top as a narrower rectangle. A
    // switch lays its OWN bed inside drawTrackWithSwitches — the selected route
    // full, an unselected arm cut back to a short stub — so the blanket bed
    // below is only for plain (non-switch) track.
    if (isSwitch) {
      this.drawTrackWithSwitches(trackBits, half, trackColor, switchState, isDiagonalOnly, switchPoints);
    } else {
      this.drawTrackBed(trackBits, half);
      this.drawTrackFromBits(trackBits, half, trackColor, track.dead_end_ports || [], connectedEdges);
    }

    this.ctx.restore();

    // NOTE: the switch M/D badge is NOT drawn here. It is drawn in a separate
    // pass after every tile's rails (renderer _drawWorld -> drawSwitchBadgeAt),
    // so a neighbouring tile's rail or bed can't paint over it, and it is
    // unclipped so it sits flush against the branch root (the switch entry edge).

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
   * The caps are square to the path, so the caller controls exactly where the
   * band ends: place `start`/`end` ON the edge points for a square end that stops
   * at the tile border, or PAST them so the tile clip (drawTrack) trims the band
   * flush to the diamond. Both the grey bed (drawTrackBed) and the block-status
   * color (drawTrackFromBits) build their per-segment bands this way.
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
    connectedEdges: Set<string> = new Set(),
  ): void {
    const p = getCanvasPalette();
    const edgePoints = this.getEdgePoints(half);

    const segments = getActiveSegments(trackBits);

    // Lay the block-status color on top of the grey bed using the SAME geometry
    // as the bed (drawTrackBed) — a filled band per segment, as wide as the bed,
    // and footprint-matched (axial √2 wider) so every segment's
    // border footprint is equal and the bands tile with no spike. Each end is
    // EITHER extended past its edge point so the tile clip (set by drawTrack)
    // trims it flush to the border, OR pulled back inside to a square end:
    //   - A same-block edge (in connectedEdges) EXTENDS: the clip chamfers it
    //     along the shared border and the neighbour's band meets it there, so a
    //     straight run stays continuous, a bend turns a clean solid corner, and
    //     two segments of a crossing fill solid instead of leaving a star.
    //   - Every other edge (a different block, or empty) is pulled `capInset`
    //     inside to a square end that stops short of the border, so one block's
    //     color never bleeds into the next and a grey gap marks the boundary.
    // (Filling each segment, rather than stroking a joined polyline, lets two
    // same-color bands overlap cleanly — the source of the old corner triangles.)
    const capInset = TRACK_WIDTH * this.scale;
    for (const [e1, e2] of segments) {
      const a = edgePoints[e1];
      const b = edgePoints[e2];
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const isAxial = OPPOSITE_EDGE[e1] === e2;
      const bandWidth = (isAxial ? TRACK_WIDTH * 2 : TRACK_WIDTH * Math.SQRT2) * this.scale;
      const start = connectedEdges.has(e1)
        ? { x: a.x - ux * half, y: a.y - uy * half }      // extend; clip trims to border
        : { x: a.x + ux * capInset, y: a.y + uy * capInset }; // square end inside the border
      const end = connectedEdges.has(e2)
        ? { x: b.x + ux * half, y: b.y + uy * half }
        : { x: b.x - ux * capInset, y: b.y - uy * capInset };
      this.drawCenterlineBand(start, end, bandWidth, color);
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

    // Whether a present segment is the selected (active) route. A diagonal-only
    // switch takes the clockwise MAIN/DIVERGE pick computed above; a regular
    // switch's long NS/EW segment is MAIN (state 0) and its diagonals are DIVERGE
    // (state 1).
    const isSegmentActive = (seg: { bit: number }): boolean => {
      if (isDiagonalOnly) return activeSegmentBits.has(seg.bit);
      const isLongSegment = seg.bit <= 1;
      return (isLongSegment && switchState === 0) || (!isLongSegment && switchState === 1);
    };

    // The edges the selected route leaves by. An unselected arm meets the
    // selected route at the switch on whichever of its two ends is one of these
    // edges; that shared end is the "switch side" the gap opens at (below).
    const activeEdges = new Set<string>();
    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;
      if (isSegmentActive(seg)) {
        activeEdges.add(seg.edges[0]);
        activeEdges.add(seg.edges[1]);
      }
    }

    // Bed pass — the grey roadbed. A switch lays its bed HERE (not via the
    // blanket drawTrackBed) so an unselected arm can be drawn SHORT. The selected
    // route gets its full bed; an unselected arm is cut back to a stub at its
    // outer end, so the switch side is simply never painted. Cutting by NOT
    // drawing (rather than painting then erasing) leaves no stray bed slivers to
    // notch into triangles at the junction. Each drawn end is EXTENDED past its
    // edge point so the tile clip (set by drawTrack) trims it flush to the
    // diamond border.
    const extendPast = (pt: EdgePoint, away: EdgePoint): EdgePoint => {
      const dx = pt.x - away.x;
      const dy = pt.y - away.y;
      const len = Math.hypot(dx, dy) || 1;
      return { x: pt.x + (dx / len) * half, y: pt.y + (dy / len) * half };
    };
    // Fraction of an unselected arm removed, measured from the switch side; the
    // remaining third is kept as a stub pointing at where the route continues.
    const CUT_FRACTION = 2 / 3;
    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;
      const [e1, e2] = seg.edges;
      const a = edgePoints[e1];
      const b = edgePoints[e2];
      if (!a || !b) continue;
      const isAxial = OPPOSITE_EDGE[e1] === e2;
      const bedWidth = (isAxial ? TRACK_WIDTH * 2 : TRACK_WIDTH * Math.SQRT2) * this.scale;

      if (isSegmentActive(seg)) {
        this.drawCenterlineBand(extendPast(a, b), extendPast(b, a), bedWidth, p.track);
        continue;
      }
      // Unselected: keep only the outer third nearest the neighbour. The switch
      // side is the end shared with the selected route; an arm that shares no end
      // (the other diagonal of a crossing switch) is cut whole — no stub.
      const e1IsSwitch = activeEdges.has(e1);
      const e2IsSwitch = activeEdges.has(e2);
      if (e1IsSwitch === e2IsSwitch) continue;
      const switchEnd = e1IsSwitch ? a : b;
      const farEnd = e1IsSwitch ? b : a;
      const cut = {
        x: farEnd.x + (switchEnd.x - farEnd.x) * (1 - CUT_FRACTION),
        y: farEnd.y + (switchEnd.y - farEnd.y) * (1 - CUT_FRACTION),
      };
      this.drawCenterlineBand(extendPast(farEnd, switchEnd), cut, bedWidth, p.track);
    }

    for (const seg of SEGMENTS) {
      if (!(trackBits & (1 << seg.bit))) continue;

      const [e1, e2] = seg.edges;
      const start = edgePoints[e1];
      const end = edgePoints[e2];
      if (!start || !end) continue;

      if (isSegmentActive(seg)) {
        // Colour pass — the selected arm, laid over its bed from the pass above
        // with the SAME extend-and-clip geometry: footprint-matched (axial √2
        // wider) and extended past the edge points so the tile clip trims it
        // flush to the border, but narrower than the bed so the grey still frames
        // it. Extending lets the clip chamfer the ends to the diamond instead of
        // leaving a square cap the clip cuts into corner triangles, and two
        // active arms of a crossing overlap solid instead of leaving a notched
        // star.
        const isAxial = OPPOSITE_EDGE[e1] === e2;
        const segmentWidth = (isAxial ? TRACK_WIDTH * Math.SQRT2 : TRACK_WIDTH) * this.scale;
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = (dx / len) * half;
        const uy = (dy / len) * half;
        this.drawCenterlineBand(
          { x: start.x - ux, y: start.y - uy },
          { x: end.x + ux, y: end.y + uy },
          segmentWidth,
          color,
        );
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
  /**
   * The edge the selected route leaves by, for the current switch_state. A
   * regular switch's MAIN route (state 0) runs straight through to the edge
   * opposite the entry; its DIVERGE route (state 1) takes the other listed exit.
   * A diagonal-only switch has no straight-through, so it picks the first or
   * second exit in clockwise order (matching drawTrackWithSwitches). Returns
   * null when the switch lists no exits.
   */
  selectedExitEdge(entryEdge: string, exits: string[], switchState: number, isDiagonalOnly: boolean): string | null {
    if (!exits || exits.length === 0) return null;
    if (isDiagonalOnly) {
      const CLOCKWISE = ['N', 'E', 'S', 'W'];
      const sorted = [...exits].sort((a, b) => CLOCKWISE.indexOf(a) - CLOCKWISE.indexOf(b));
      return switchState === 0 ? sorted[0] : (sorted[1] ?? sorted[0]);
    }
    const mainExit = OPPOSITE_EDGE[entryEdge];
    if (switchState === 0 && exits.includes(mainExit)) return mainExit;
    return exits.find((e) => e !== mainExit) ?? exits[0];
  }

  /**
   * The screen point at the root of a switch's branches, ON the selected
   * route's rail. The entry edge (first switch_points key) is the trunk corner
   * where the two arms split and the short stub peels off — the branch root. The
   * dot sits a FIXED step in from that corner, ALONG the selected route. A fixed
   * distance (not a fraction of the route) keeps every dot the same distance
   * from its root, whether the route is a long axial run or a short diagonal
   * arm. Going along the route (rather than toward the tile centre) keeps it on
   * the rail's centreline: an axial route runs through the tile centre, but a
   * diagonal route runs along a tile-box edge that does NOT, so pulling the
   * anchor toward the centre would drift it off a diagonal rail.
   *
   * Exception: a crossover/crossing switch connects every edge (more than one
   * entry in switch_points). Its routes cross at the tile centre and there is no
   * single trunk corner, so the dot belongs at the centre.
   *
   * The M/D badge and the lock/occupied dot share this anchor.
   */
  switchRootAnchor(
    half: number,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): EdgePoint {
    const edgePoints = this.getEdgePoints(half);
    const entryEdges = Object.keys(switchPoints);
    if (entryEdges.length > 1) return { x: 0, y: 0 }; // crossover/crossing: centre of the tile
    const entryEdge = entryEdges[0];
    const entryPt = entryEdge ? edgePoints[entryEdge] : null;
    if (!entryPt || !entryEdge) return { x: 0, y: 0 };
    const exitEdge = this.selectedExitEdge(entryEdge, switchPoints[entryEdge] || [], switchState, isDiagonalOnly);
    const exitPt = exitEdge ? edgePoints[exitEdge] : null;
    if (!exitPt) return { x: entryPt.x * 0.7, y: entryPt.y * 0.7 };
    const dx = exitPt.x - entryPt.x;
    const dy = exitPt.y - entryPt.y;
    const len = Math.hypot(dx, dy) || 1;
    // Step in from the root corner by exactly one dot radius, along the rail.
    // That keeps the dot at the trunk mouth, but far enough from the tile edge
    // that two switches facing each other across a shared edge end up two radii
    // apart — touching at most, never overlapping into a peanut. Fixed (not a
    // fraction of the route) so every dot sits the same distance from its root.
    const rootInset = TRACK_WIDTH * 1.25 * this.scale; // = the dot radius
    return {
      x: entryPt.x + (dx / len) * rootInset,
      y: entryPt.y + (dy / len) * rootInset,
    };
  }

  drawSwitchBadge(
    half: number,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): void {
    const p = getCanvasPalette();
    const { x: bx, y: by } = this.switchRootAnchor(half, switchState, isDiagonalOnly, switchPoints);

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
  // cannot build here (e.g. a forced segment that
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
   * and the block drag preview). Filling each tile diamond makes a run
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
