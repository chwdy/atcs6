// renderer-tracks.ts — draws track tiles (straight/diagonal segments, switches
// with their state badge + lock/occupied dot, dead-ends, crossings) and the editor
// preview overlays. Owns the shared getEdgePoints() / drawGridSegment() geometry
// that the other renderer handlers reach for through the renderer.
//
// Covered by renderer-tracks.test.ts.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererTrackHandler(this)`).

import { getActiveSegments, trackBitsToEdges, isCrossing, TRACK_WIDTH, TRACK_LINE_WIDTH, TRACK_STATUS_WIDTH, OPPOSITE_EDGE } from '../lib/constants';
import { getBlockStateColor, type BlockLiveFields } from '../lib/helpers';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';
import { traceTrackLine } from './track-path';

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

/** What the network payload says about a switch's condition
 *  (design_v2.md "Switches"). */
export interface SwitchCondition {
  /** A train is on it or about to cross it, so nobody throws it. */
  held?: boolean;
  /** Marked manual: the runtime never throws it for a train. */
  manual?: boolean;
  /** Set while alternate arrivals are on for the switch. */
  alternate_next?: number | null;
  /** False until the switch has been thrown this session. */
  position_known?: boolean;
}

/** The one mark drawn beside a switch, or null for a plain switch. A switch
 *  can be in several conditions at once; the mark shown is the one that
 *  matters most to someone about to throw it. */
export function switchMark(sw: SwitchCondition | undefined): 'held' | 'unknown' | 'manual' | 'alternate' | null {
  if (!sw) return null;
  if (sw.held) return 'held';
  if (sw.position_known === false) return 'unknown';
  if (sw.manual) return 'manual';
  if (sw.alternate_next != null) return 'alternate';
  return null;
}

// Tabler icon font glyphs (static/vendor/tabler): lock, hand-stop, refresh.
const SWITCH_MARK_GLYPH: Record<string, string> = { held: '\ueae2', manual: '\uec2e', alternate: '\ueb13' };

/** The slice of the Renderer the track handler reads through its back-reference. */
type TrackBlock = BlockLiveFields;

export interface TrackHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, TrackBlock | undefined> | null;
  currentNetwork?: { operation_mode?: string; switches?: Record<string, SwitchCondition | undefined> } | null;
  // "x,y" -> track, used by drawTileExtentHighlight to read each selected
  // tile's track_bits so it can light the real stroke (not just the cell).
  trackMap?: Record<string, { track_bits?: number; block_section_id?: string | null } | undefined>;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  debugHandler: { highlightDeadEnds(x: number, y: number, deadEnds: string[], trackBits?: number): void };
}

/**
 * Track fill color for a block, by its live fields (design_v2.md "Live
 * fields"): the detector reading first, then a claim, which shows the
 * caution color while another train is in the block beyond (design_v2.md
 * "Seeing trains on the layout"). A train's block that
 * does not read occupied is not painted occupied, so the track shows what the
 * detector sees. Returns null when the block is absent, so the caller keeps
 * its default track color.
 */
export function blockTrackColor(block: TrackBlock | undefined | null): string | null {
  if (!block) return null;
  // Consume the --block-* design tokens via the canonical resolver, the same
  // path renderer-blocks / train-monitor use (DESIGN_LANGUAGE.md: "Track / block
  // renderers consume these tokens; do not hard-code hex literals at the call
  // site").
  return getBlockStateColor(
    block.reads_occupied
      ? 'occupied'
      : block.claimed_by_train_id
        ? block.train_ahead
          ? 'caution'
          : 'reserved'
        : 'free',
  );
}

/** One tile segment: its two tile edges by shared name, where they are on
 *  screen, and the tile it belongs to. */
export type LinePiece = { a: string; b: string; pa: EdgePoint; pb: EdgePoint; tile: string };
type LinkedLine = { points: EdgePoint[]; closed: boolean; startFree: boolean; endFree: boolean };

function towards(from: EdgePoint, to: EdgePoint, distance: number): EdgePoint {
  const len = Math.hypot(to.x - from.x, to.y - from.y) || 1;
  const t = Math.min(distance / len, 0.5);
  return { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
}

/** The name of a tile's edge. An edge is shared with the neighbour across
 *  it, so both tiles give it the same name and their segments link there. */
function edgeName(x: number, y: number, edge: string): string {
  if (edge === 'N') return `${x},${y},N`;
  if (edge === 'S') return `${x},${y - 1},N`;
  if (edge === 'E') return `${x},${y},E`;
  return `${x - 1},${y},E`;
}

const EDGE_OFFSET: Record<string, [number, number]> = { N: [1, -1], E: [1, 1], S: [-1, 1], W: [-1, -1] };

/** Link pieces that meet at an edge shared by exactly two of them into lines.
 *  A line ends where it meets no other piece (a free end) or where three or
 *  more pieces meet. What is left over after that are closed loops.
 *
 *  Where three or more pieces meet, each line carries on along one piece of
 *  the neighbouring tile, so the canvas joins it to that piece instead of
 *  leaving a square end with a notch beside it. The extra length lies on top
 *  of track the same stroke already draws. */
export function linkPieces(pieces: LinePiece[]): LinkedLine[] {
  const at = new Map<string, number[]>();
  pieces.forEach((piece, i) => {
    for (const name of [piece.a, piece.b]) {
      const list = at.get(name);
      if (list) list.push(i);
      else at.set(name, [i]);
    }
  });
  const used: boolean[] = pieces.map(() => false);
  // The far end of a piece of another tile that meets `name`, or null.
  const beyond = (name: string, tile: string): EdgePoint | null => {
    const meeting = at.get(name) ?? [];
    if (meeting.length < 3) return null;
    const other = meeting.map((i) => pieces[i]).find((piece) => piece.tile !== tile);
    if (!other) return null;
    return other.a === name ? other.pb : other.pa;
  };
  const carryOn = (points: EdgePoint[], name: string, tile: string): void => {
    const point = beyond(name, tile);
    if (point) points.push(point);
  };
  const walk = (first: number, from: string): LinkedLine => {
    let i = first;
    let name = from;
    const points = [name === pieces[i].a ? pieces[i].pa : pieces[i].pb];
    const lead = beyond(from, pieces[first].tile);
    if (lead) points.unshift(lead);
    for (;;) {
      used[i] = true;
      const piece = pieces[i];
      const forward = name === piece.a;
      points.push(forward ? piece.pb : piece.pa);
      name = forward ? piece.b : piece.a;
      const meeting = at.get(name) ?? [];
      if (meeting.length !== 2) {
        carryOn(points, name, piece.tile);
        return { points, closed: false, startFree: at.get(from)?.length === 1, endFree: meeting.length === 1 };
      }
      const next = meeting[0] === i ? meeting[1] : meeting[0];
      if (used[next]) {
        points.pop();
        return { points, closed: true, startFree: false, endFree: false };
      }
      i = next;
    }
  };
  const lines: LinkedLine[] = [];
  for (const [name, meeting] of at) {
    if (meeting.length === 2) continue;
    for (const i of meeting) if (!used[i]) lines.push(walk(i, name));
  }
  pieces.forEach((piece, i) => {
    if (!used[i]) lines.push(walk(i, piece.a));
  });
  return lines;
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
  get currentNetwork(): TrackHandlerRenderer['currentNetwork'] {
    return this.renderer.currentNetwork;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
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

  /** Which arms of a switch tile to draw: the selected route whole, and an
   *  unselected arm as a leg drawn from its outer edge (`stubFrom`) that
   *  stops short of the selected route. A
   *  regular switch's straight-through segment is the route for state 0 and
   *  its turning segments are the route for state 1. A diagonal-only switch
   *  has no straight-through, so it takes the first exit clockwise for state
   *  0 and the second for state 1. An unselected arm that shares no edge
   *  with the selected route is not drawn. */
  switchArms(
    trackBits: number,
    switchState: number,
    isDiagonalOnly: boolean,
    switchPoints: Record<string, string[]>,
  ): Array<{ e1: string; e2: string; stubFrom: string | null }> {
    const segments = getActiveSegments(trackBits);
    const CLOCKWISE = ['N', 'E', 'S', 'W'];
    const isActive = ([e1, e2]: [string, string]): boolean => {
      if (!isDiagonalOnly) {
        const isLong = OPPOSITE_EDGE[e1] === e2;
        return isLong ? switchState === 0 : switchState === 1;
      }
      return Object.entries(switchPoints).some(([entry, exits]) => {
        if (!exits || exits.length < 2) return false;
        const sorted = [...exits].sort((a, b) => CLOCKWISE.indexOf(a) - CLOCKWISE.indexOf(b));
        const exit = switchState === 0 ? sorted[0] : sorted[1];
        return (e1 === entry && e2 === exit) || (e2 === entry && e1 === exit);
      });
    };
    const activeEdges = new Set<string>();
    for (const seg of segments) if (isActive(seg)) seg.forEach((e) => activeEdges.add(e));
    const arms: Array<{ e1: string; e2: string; stubFrom: string | null }> = [];
    for (const seg of segments) {
      const [e1, e2] = seg;
      if (isActive(seg)) {
        arms.push({ e1, e2, stubFrom: null });
        continue;
      }
      const e1IsSwitch = activeEdges.has(e1);
      if (e1IsSwitch === activeEdges.has(e2)) continue;
      arms.push({ e1, e2, stubFrom: e1IsSwitch ? e2 : e1 });
    }
    return arms;
  }

  /**
   * Draw all track as lines of one thickness. Tile segments that meet on a
   * shared tile edge are linked into one line, traced with rounded bends
   * (track-path.ts), so horizontal, vertical and 45° track are equally thick.
   *
   * All track is drawn in the quiet track colour. Each block's status colour
   * is then drawn as a thin line along the middle of its track, which stops
   * TRACK_WIDTH short of each end of the block, so two blocks' lines never
   * touch.
   *
   * A switch tile draws its selected route whole and the leg not taken in
   * the dim track colour, stopping just short of the selected route. While
   * alternate arrivals are on, both legs are drawn whole, because the runtime
   * picks the leg. Until the switch's position is known, both legs are dim.
   */
  /** Where the middle of a tile's edge is on screen. */
  edgeOnScreen(x: number, y: number, edge: string): EdgePoint {
    const quarter = (this.gridSize * this.scale) / 4;
    return {
      x: this.gridToScreenX(x, y) + EDGE_OFFSET[edge][0] * quarter,
      y: this.gridToScreenY(x, y) + EDGE_OFFSET[edge][1] * quarter,
    };
  }

  /** One segment of a tile, from edge `e1` to edge `e2`, ready to be linked
   *  with its neighbours' (linkPieces). */
  tilePiece(x: number, y: number, e1: string, e2: string): LinePiece {
    return {
      a: edgeName(x, y, e1),
      b: edgeName(x, y, e2),
      pa: this.edgeOnScreen(x, y, e1),
      pb: this.edgeOnScreen(x, y, e2),
      tile: `${x},${y}`,
    };
  }

  drawTracks(tracks: Track[]): void {
    const p = getCanvasPalette();
    const bed: LinePiece[] = [];
    const notTaken: EdgePoint[][] = [];
    const colored = new Map<string, { color: string; width: number; inset: number; pieces: LinePiece[] }>();
    const addColored = (key: string, color: string, width: number, inset: number, piece: LinePiece) => {
      let group = colored.get(key);
      if (!group) colored.set(key, group = { color, width, inset, pieces: [] });
      group.pieces.push(piece);
    };
    const lineWidth = TRACK_LINE_WIDTH * this.scale;
    const statusWidth = TRACK_STATUS_WIDTH * this.scale;
    const crossings: EdgePoint[] = [];

    for (const track of tracks) {
      const bits = track.track_bits ?? 0;
      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);
      const point = (edge: string): EdgePoint => this.edgeOnScreen(track.x, track.y, edge);
      const piece = (e1: string, e2: string): LinePiece => this.tilePiece(track.x, track.y, e1, e2);
      let color = p.track;
      if (track.block_section_id && this.currentBlocks) {
        color = blockTrackColor(this.currentBlocks[track.block_section_id]) || color;
      }

      if (track.is_switch) {
        const arms = this.switchArms(
          bits, track.switch_state || 0, track.is_diagonal_only_switch || false, track.switch_points || {},
        );
        const mark = switchMark(this.currentNetwork?.switches?.[track.switch_id ?? '']);
        if (mark === 'unknown') {
          // No leg is known to be set: every leg is dim, edge to edge.
          for (const [e1, e2] of getActiveSegments(bits)) {
            const a = point(e1);
            const b = point(e2);
            const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
            const nx = (-(b.y - a.y) / length) * (lineWidth / 2);
            const ny = ((b.x - a.x) / length) * (lineWidth / 2);
            notTaken.push([
              { x: a.x + nx, y: a.y + ny }, { x: a.x - nx, y: a.y - ny },
              { x: b.x - nx, y: b.y - ny }, { x: b.x + nx, y: b.y + ny },
            ]);
          }
          continue;
        }
        if (mark === 'alternate') {
          for (const [e1, e2] of getActiveSegments(bits)) bed.push(piece(e1, e2));
          continue;
        }
        arms.forEach(({ e1, e2, stubFrom }, i) => {
          if (!stubFrom) {
            bed.push(piece(e1, e2));
            if (color !== p.track) addColored(`switch|${color}`, color, statusWidth, 0, piece(e1, e2));
            return;
          }
          // The leg not taken: full width, in the dim track colour, from
          // its outer edge to just short of the selected route. Its end is
          // cut parallel to that route, so a strip of the ground of one
          // width shows between the two.
          const nearEdge = stubFrom === e1 ? e2 : e1;
          const far = point(stubFrom);
          const near = point(nearEdge);
          const length = Math.hypot(far.x - near.x, far.y - near.y) || 1;
          const ux = (far.x - near.x) / length;
          const uy = (far.y - near.y) / length;
          const halfWidth = lineWidth / 2;
          const clearance = halfWidth + (TRACK_WIDTH / 2) * this.scale;
          // How far from the shared edge each side of the leg stops: where
          // that side meets the line drawn `clearance` away from the middle
          // of the selected route.
          const stops = [0, 0];
          for (const arm of arms) {
            if (arm.stubFrom || (arm.e1 !== nearEdge && arm.e2 !== nearEdge)) continue;
            const other = point(arm.e1 === nearEdge ? arm.e2 : arm.e1);
            const otherLength = Math.hypot(other.x - near.x, other.y - near.y) || 1;
            const vx = (other.x - near.x) / otherLength;
            const vy = (other.y - near.y) / otherLength;
            const along = ux * vx + uy * vy;
            // The direction straight away from the selected route, on the
            // leg's side of it.
            let mx = ux - along * vx;
            let my = uy - along * vy;
            const sin = Math.hypot(mx, my);
            if (sin < 0.1) continue;
            mx /= sin;
            my /= sin;
            const across = -uy * mx + ux * my;
            [1, -1].forEach((side, k) => {
              stops[k] = Math.max(stops[k], (clearance - side * halfWidth * across) / sin);
            });
          }
          const limit = length * 0.85;
          const [stopA, stopB] = stops.map((stop) => Math.min(stop, limit));
          const corner = (side: number, stop: number): EdgePoint => ({
            x: near.x - uy * side * halfWidth + ux * stop,
            y: near.y + ux * side * halfWidth + uy * stop,
          });
          notTaken.push([corner(1, length), corner(-1, length), corner(-1, stopB), corner(1, stopA)]);
          // Grey under the leg as far as its shorter side, so the canvas
          // joins the leg to the track beyond its outer edge.
          const greyStop = Math.max(stopA, stopB);
          bed.push({
            a: edgeName(track.x, track.y, stubFrom),
            b: `cut:${track.x},${track.y},${i}`,
            pa: far,
            pb: { x: near.x + ux * greyStop, y: near.y + uy * greyStop },
            tile: `${track.x},${track.y}`,
          });
        });
        continue;
      }

      for (const [e1, e2] of getActiveSegments(bits)) {
        bed.push(piece(e1, e2));
        if (color !== p.track) {
          addColored(`${track.block_section_id}|${color}`, color, statusWidth, TRACK_WIDTH * this.scale, piece(e1, e2));
        }
      }
      if (isCrossing(bits)) crossings.push({ x: cx, y: cy });
    }

    this.ctx.save();
    this.ctx.lineCap = 'butt';
    this.ctx.lineJoin = 'miter';
    this.strokeLinePieces(bed, lineWidth, p.track, 0);
    this.ctx.fillStyle = p.trackDim;
    for (const corners of notTaken) {
      this.ctx.beginPath();
      corners.forEach((c, i) => (i === 0 ? this.ctx.moveTo(c.x, c.y) : this.ctx.lineTo(c.x, c.y)));
      this.ctx.closePath();
      this.ctx.fill();
    }
    this.ctx.lineCap = 'round';
    for (const group of colored.values()) {
      this.strokeLinePieces(group.pieces, group.width, group.color, group.inset);
    }
    this.ctx.fillStyle = p.accent;
    for (const c of crossings) {
      this.ctx.beginPath();
      this.ctx.arc(c.x, c.y, 4 * this.scale, 0, Math.PI * 2);
      this.ctx.fill();
    }
    this.ctx.restore();

    for (const track of tracks) {
      if (track.dead_end_ports && track.dead_end_ports.length > 0) {
        this.renderer.debugHandler.highlightDeadEnds(track.x, track.y, track.dead_end_ports, track.track_bits || 0);
      }
    }
  }

  /** Link the pieces into lines and stroke them all in one stroke, so lines
   *  of one colour that overlap do not darken each other. An open end where
   *  the line simply stops is pulled back by `inset`. */
  strokeLinePieces(pieces: LinePiece[], width: number, color: string, inset: number): void {
    if (pieces.length === 0) return;
    this.ctx.lineWidth = width;
    this.ctx.strokeStyle = color;
    this.ctx.beginPath();
    for (const { points, closed, startFree, endFree } of linkPieces(pieces)) {
      const pts = points.slice();
      if (!closed && inset > 0) {
        if (startFree) pts[0] = towards(pts[0], pts[1], inset);
        if (endFree) pts[pts.length - 1] = towards(pts[pts.length - 1], pts[pts.length - 2], inset);
      }
      traceTrackLine(this.ctx, pts, closed, this.scale);
    }
    this.ctx.stroke();
  }

  /**
   * The mark beside each switch that is not plain: a small round badge, off
   * the track, on the side of the tile no track leaves by. A lock for a held
   * switch, a hand for one marked manual, circling arrows while alternate
   * arrivals are on, and a question mark until its position is known. The
   * held badge is drawn in the train line's colour, because a train holds it.
   */
  drawSwitchMarks(tracks: Track[]): void {
    const switches = this.currentNetwork?.switches;
    if (!switches) return;
    const p = getCanvasPalette();
    const quarter = (this.gridSize * this.scale) / 4;
    const OFFSET: Record<string, [number, number]> = { N: [1, -1], E: [1, 1], S: [-1, 1], W: [-1, -1] };
    const radius = Math.max(7, Math.min(4.5 * this.scale, 11));
    this.ctx.save();
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    for (const track of tracks) {
      if (!track.is_switch || !track.switch_id) continue;
      const mark = switchMark(switches[track.switch_id]);
      if (!mark) continue;
      const used = trackBitsToEdges(track.track_bits ?? 0);
      const free = ['N', 'E', 'S', 'W'].find((edge) => !used.has(edge));
      const [dx, dy] = free ? OFFSET[free] : [0, -1.4];
      const x = this.gridToScreenX(track.x, track.y) + dx * quarter * 1.7;
      const y = this.gridToScreenY(track.x, track.y) + dy * quarter * 1.7;
      const ink = mark === 'held' ? p.trainLine : p.textDim;
      this.ctx.beginPath();
      this.ctx.arc(x, y, radius, 0, Math.PI * 2);
      this.ctx.fillStyle = p.bg;
      this.ctx.fill();
      this.ctx.lineWidth = 1.5;
      this.ctx.strokeStyle = ink;
      this.ctx.stroke();
      this.ctx.fillStyle = ink;
      const glyph = SWITCH_MARK_GLYPH[mark];
      this.ctx.font = glyph ? `${Math.round(radius * 1.3)}px "tabler-icons"` : `600 ${Math.round(radius * 1.3)}px sans-serif`;
      this.ctx.fillText(glyph ?? '?', x, y + 0.5);
    }
    this.ctx.restore();
  }

  /**
   * The edge the selected route leaves by, for the current switch_state. A
   * regular switch's MAIN route (state 0) runs straight through to the edge
   * opposite the entry; its DIVERGE route (state 1) takes the other listed exit.
   * A diagonal-only switch has no straight-through, so it picks the first or
   * second exit in clockwise order (matching switchArms). Returns
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

  /**
   * The track the track tool is about to lay: drawn the way track is drawn,
   * as one line as thick as the track with round bends, in the build-preview
   * colour. One stroke for all of it, so the see-through colour is even where
   * two tiles meet.
   */
  drawPreviewTracks(tracks: Track[] | null | undefined): void {
    if (!tracks || tracks.length === 0) {
      return;
    }

    const p = getCanvasPalette();
    const pieces: LinePiece[] = [];
    const crossings: EdgePoint[] = [];
    for (const track of tracks) {
      const trackBits = track.track_bits ?? 0;
      if (trackBits === 0) continue;
      for (const [e1, e2] of getActiveSegments(trackBits)) {
        pieces.push(this.tilePiece(track.x, track.y, e1, e2));
      }
      if (isCrossing(trackBits)) {
        crossings.push({ x: this.gridToScreenX(track.x, track.y), y: this.gridToScreenY(track.x, track.y) });
      }
    }

    this.ctx.save();
    this.ctx.lineCap = 'butt';
    this.ctx.lineJoin = 'round';
    this.strokeLinePieces(pieces, TRACK_LINE_WIDTH * this.scale, withAlpha(p.caution, 0.8), 0);
    this.ctx.fillStyle = withAlpha(p.caution, 0.9);
    for (const c of crossings) {
      this.ctx.beginPath();
      this.ctx.arc(c.x, c.y, 4 * this.scale, 0, Math.PI * 2);
      this.ctx.fill();
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
