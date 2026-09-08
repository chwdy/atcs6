// renderer-trains.ts — draws each train: body polyline (tail→head, split at
// tunnel hops), head/tail/end markers, the "stuck waiting" debug arrow, and the
// track-dodging train label.
//
// Migrated from static/js/renderer-trains.js (longterm_todo.md item #7,
// renderer cluster — the largest handler). Behavior is unchanged; covered by
// renderer-trains.test.ts.
//
// Imports getLabelSpacingMultiplier and DIRECTION_PREFERRED_QUADRANTS from
// ../lib/constants, and still reads stateManager (the snapshot fallback).
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererTrainHandler(this)`).

import { stateManager } from '../lib/state-manager';
import {
  BIT_TO_EDGES,
  OPPOSITE_EDGE,
  TRACK_NS,
  TRACK_EW,
  TRACK_NE,
  TRACK_NW,
  TRACK_SE,
  TRACK_SW,
  getLabelSpacingMultiplier,
  DIRECTION_PREFERRED_QUADRANTS,
} from '../lib/constants';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';

// Train body fill, chosen by EB / route-teardown / control-mode state. The
// dark border (the canvas ink color) is unchanged; only the inner fill is
// tinted so the head/tail markers stay readable. EB wins over everything — an
// EB'd train must be findable at a glance; teardown wins over manual when both
// apply. Colors resolve at draw time from the active canvas palette so the body
// follows the canvas theme.
export function trainBodyFill(snap: {
  controlMode?: string | null;
  teardownPending?: boolean;
  ebActive?: boolean;
  isUnidentified?: boolean;
}): string {
  const p = getCanvasPalette();
  // An unidentified train is not one of the operator's trains: it gets a desaturated
  // steel body (plus the dashed outline drawn with it), never a mode hue, since
  // it has no cab, no mode and no brake to report.
  if (snap.isUnidentified) return p.trainBodyUnidentified;
  if (snap.ebActive) return p.trainBodyEb;
  if (snap.teardownPending) return p.trainBodyTeardown;
  if (snap.controlMode === 'manual') return p.trainBodyManual;
  return p.trainBody;
}

interface SpanEndpoint {
  tile: [number, number];
  distance_cm: number;
  entry_edge: string;
}

interface TrainSnapSpan {
  occupiedTiles: Array<[number, number]>;
  leadingEnd: string;
  endA: SpanEndpoint;
  endB: SpanEndpoint;
}

export interface TrainSnap {
  id: string;
  cab: string | null;
  facing: string | null;
  /** 'manual' | 'ato' (or null). Drives the manual-mode body tint. */
  controlMode?: string | null;
  /** True while an operator route teardown (abort/deactivate) is latched. */
  teardownPending?: boolean;
  /** True while the emergency brake is active. Drives the purple body tint. */
  ebActive?: boolean;
  /** True for a runtime-detected unidentified train (design_new.md "Unidentified trains").
   *  Drives the steel body tint and the dashed body outline. */
  isUnidentified?: boolean;
  /** True while no wired block backs the calculated position — the train may
   *  physically be gone (design_new.md "Power", Presence unknown). Drives the
   *  question-mark badge at the head. */
  presenceUnknown?: boolean;
  span: TrainSnapSpan;
  waitingForBlockId?: string | null;
}

export interface Train {
  id: string;
  num_cars?: number;
  is_unidentified?: boolean;
}

interface TrainTrack {
  x: number;
  y: number;
  track_bits?: number;
  block_section_id?: string | null;
  is_switch?: boolean;
  switch_state?: number;
  switch_id?: string;
}

interface TunnelEndpointRec {
  x: number;
  y: number;
  edge: string;
}

interface TunnelPairRec {
  a: TunnelEndpointRec;
  b: TunnelEndpointRec;
}

interface TrainNetwork {
  tracks?: TrainTrack[];
  blocks?: Record<string, { length_cm?: number } | undefined>;
  switches?: Record<string, { main_length_cm?: number; diverge_length_cm?: number } | undefined>;
  tunnels?: TunnelPairRec[];
}

interface Point {
  x: number;
  y: number;
}

interface LabelBound {
  x: number;
  y: number;
  w: number;
  h: number;
  tier: string;
}

/**
 * End-marker square, in px at scale 1. The unidentified-body inset is measured
 * against this same constant, so the square lands exactly in the gap the body
 * gives up — keep the two reading from here rather than re-deriving either.
 */
const END_MARKER_SIZE = 10;

/**
 * Shorten a body polyline by `inset` screen px at one end, walking inward
 * through as many segments as the inset consumes (a half-tile segment is
 * usually longer than the inset, but a span ending mid-switch can be shorter).
 * Pure — returns a new array.
 *
 * A polyline shorter than the inset comes back untouched: a body that would
 * have to fold through itself is worse than one drawn at full length.
 */
export function trimPolylineEnd(points: Point[], inset: number, fromStart: boolean): Point[] {
  if (points.length < 2 || inset <= 0) return points;
  const pts = fromStart ? [...points] : [...points].reverse();
  let remaining = inset;
  while (pts.length >= 2) {
    const dx = pts[1].x - pts[0].x;
    const dy = pts[1].y - pts[0].y;
    const len = Math.hypot(dx, dy);
    if (len > remaining) {
      pts[0] = {
        x: pts[0].x + (dx / len) * remaining,
        y: pts[0].y + (dy / len) * remaining,
      };
      return fromStart ? pts : pts.reverse();
    }
    // The inset swallows this whole segment: drop its start point and carry
    // the leftover into the next one. len === 0 (duplicate points) still
    // shifts, so the loop always makes progress.
    remaining -= len;
    pts.shift();
  }
  return points;
}

/** The slice of the Renderer the train handler reads through its back-reference. */
export interface TrainHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentNetwork?: TrainNetwork | null;
  currentSnapshot?: { trains?: Record<string, TrainSnap> } | null;
  debugFlags?: { showStuck?: boolean };
  mouseX: number;
  mouseY: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  trackHandler: { getEdgePoints(half: number): Record<string, Point> };
  getNearbyTrackSegments(cx: number, cy: number, radius: number): unknown[];
  platformHandler?: { getPlatformBodySegments?(): unknown[] };
  labelBounds: LabelBound[];
  distRectToSegments(bx: number, by: number, boxW: number, boxH: number, segments: unknown[]): number;
  aabbOverlapArea(
    ax: number, ay: number, aw: number, ah: number,
    bx: number, by: number, bw: number, bh: number,
  ): number;
}

export class RendererTrainHandler {
  renderer: TrainHandlerRenderer;
  _labelDodge?: Map<string, { dodged: boolean; mx: number; my: number }>;

  constructor(renderer: TrainHandlerRenderer) {
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
  get currentNetwork(): TrainNetwork | null | undefined {
    return this.renderer.currentNetwork;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  // ========================================================================
  // Main Entry Point
  // ========================================================================

  drawTrain(train: Train): void {
    // Read from the per-frame snapshot built by Renderer._flushRedraw
    // (frontend/src/renderer/render-snapshot.ts). Falls back to stateManager only
    // when no snapshot is available — e.g., the public drawNetwork()
    // entry called outside the rAF flush path.
    const trainSnap = this.renderer.currentSnapshot?.trains?.[train.id];
    if (trainSnap) {
      this.drawTrainFromSnapshot(train, trainSnap);
      return;
    }
    const controller = stateManager?.getController(train.id);
    if (!controller?.span?.occupied_tiles?.length) return;
    // Adapt the controller into a snapshot-shaped object so the
    // downstream renderer code only has one shape to consume.
    this.drawTrainFromSnapshot(train, {
      id: train.id,
      cab: controller.cab ?? null,
      facing: controller.facing_direction ?? null,
      controlMode: controller.control_mode ?? null,
      teardownPending: controller.route_teardown_pending === true,
      ebActive: controller.eb_active === true,
      isUnidentified: train.is_unidentified === true,
      presenceUnknown: (controller as { presence_unknown?: unknown }).presence_unknown === true,
      span: {
        occupiedTiles: controller.span.occupied_tiles,
        leadingEnd: controller.span.leading_end,
        endA: controller.span.end_a,
        endB: controller.span.end_b,
      },
    });
  }

  /**
   * Get possible exit edges for a track given an entry edge.
   * Mirrors the backend TrackSegment.get_exits() logic.
   */
  getTrackExits(
    track: { track_bits?: number; is_switch?: boolean; switch_state?: number } | null | undefined,
    entryEdge: string | null | undefined,
  ): string[] {
    if (!track || !entryEdge) return [];

    const bits = track.track_bits || 0;

    const exits: string[] = [];

    if (bits & TRACK_NS) {
      if (entryEdge === 'N') exits.push('S');
      if (entryEdge === 'S') exits.push('N');
    }
    if (bits & TRACK_EW) {
      if (entryEdge === 'E') exits.push('W');
      if (entryEdge === 'W') exits.push('E');
    }
    if (bits & TRACK_NE) {
      if (entryEdge === 'N') exits.push('E');
      if (entryEdge === 'E') exits.push('N');
    }
    if (bits & TRACK_NW) {
      if (entryEdge === 'N') exits.push('W');
      if (entryEdge === 'W') exits.push('N');
    }
    if (bits & TRACK_SE) {
      if (entryEdge === 'S') exits.push('E');
      if (entryEdge === 'E') exits.push('S');
    }
    if (bits & TRACK_SW) {
      if (entryEdge === 'S') exits.push('W');
      if (entryEdge === 'W') exits.push('S');
    }

    // For switches, apply switch state to get the active exit
    if (track.is_switch && track.switch_state !== undefined && exits.length > 1) {
      return [exits[track.switch_state] || exits[0]];
    }

    return exits;
  }

  // ========================================================================
  // Path-Based Rendering Helper Methods
  // ========================================================================

  /**
   * Get edge center point in isometric screen coordinates relative to tile center.
   */
  getEdgeCenterOffset(edge: string): Point {
    // Canonical iso-tile corner geometry lives on the track handler.
    const h = this.gridSize * this.scale / 2;
    return this.renderer.trackHandler.getEdgePoints(h)[edge] || { x: 0, y: 0 };
  }

  /**
   * Get the opposite edge.
   */
  getOppositeEdge(edge: string): string {
    return OPPOSITE_EDGE[edge] || edge;
  }

  /**
   * Exit edge a head/tail marker leaves by, having entered `track` via
   * `entryEdge`. Mirrors backend TrackSegment.get_exits(use_switch_state=True):
   * a non-switch (or a switch with only one geometric exit from this entry) has
   * an unambiguous exit; a switch with two exits picks the ACTIVE branch by
   * switch_state — the main (NS/EW) segment for state 0, the diverge (diagonal)
   * segment for state 1. For a diagonal-only switch (no NS/EW segment) the
   * branches are ordered by bit and switch_state selects the Nth. Returns null
   * when the entry edge carries no segment (caller then pins at the entry edge).
   */
  getActiveExitEdge(
    track: { track_bits?: number; is_switch?: boolean; switch_state?: number } | null | undefined,
    entryEdge: string,
  ): string | null {
    if (!track) return null;
    const bits = track.track_bits || 0;
    const candidates: Array<{ exit: string; bit: number }> = [];
    for (let bit = 0; bit < 6; bit++) {
      if (!(bits & (1 << bit))) continue;
      const [a, b] = BIT_TO_EDGES[bit];
      if (a === entryEdge) candidates.push({ exit: b, bit });
      else if (b === entryEdge) candidates.push({ exit: a, bit });
    }
    if (candidates.length === 0) return null;
    if (candidates.length === 1 || !track.is_switch) return candidates[0].exit;

    const state = track.switch_state || 0;
    const hasMain = (bits & 0b11) !== 0; // any NS/EW segment present
    if (hasMain) {
      const pick = state === 0
        ? candidates.find((c) => c.bit <= 1) // main: NS/EW
        : candidates.find((c) => c.bit >= 2); // diverge: diagonal
      return (pick || candidates[0]).exit;
    }
    // Diagonal-only switch: order diverge segments by bit, state selects.
    candidates.sort((a, b) => a.bit - b.bit);
    return (candidates[state] || candidates[0]).exit;
  }

  /**
   * Calculate screen position for a point along a tile's path.
   */
  getTilePathPosition(
    tile: [number, number],
    entryEdge: string,
    distance_cm: number,
    tile_length_cm: number,
    track: TrainTrack | null | undefined,
    exitEdgeOverride?: string,
  ): { x: number; y: number; rotation: number } {
    const x = tile[0];
    const y = tile[1];

    // Get tile center in screen coords
    const centerX = this.gridToScreenX(x, y);
    const centerY = this.gridToScreenY(x, y);

    // Get entry and exit edge offsets. A caller that already knows the exit
    // edge from the train's grid trajectory (the body/tail) passes it as
    // `exitEdgeOverride`; otherwise fall back to the track-geometry exit, which
    // reads live `switch_state` (correct for the head moving into matched track).
    const entryOffset = this.getEdgeCenterOffset(entryEdge);
    const exitEdge = exitEdgeOverride !== undefined
      ? exitEdgeOverride
      : this.getActiveExitEdge(track, entryEdge);
    const exitOffset = exitEdge ? this.getEdgeCenterOffset(exitEdge) : entryOffset;

    // Calculate progress along path (0 = entry, 1 = exit)
    const progress = tile_length_cm > 0 ? Math.min(1.0, distance_cm / tile_length_cm) : 0;

    // Interpolate between entry and exit
    const offsetX = entryOffset.x + (exitOffset.x - entryOffset.x) * progress;
    const offsetY = entryOffset.y + (exitOffset.y - entryOffset.y) * progress;

    return {
      x: centerX + offsetX,
      y: centerY + offsetY,
      rotation: Math.atan2(exitOffset.y - entryOffset.y, exitOffset.x - entryOffset.x),
    };
  }

  // ========================================================================
  // Path-Based Rendering
  // ========================================================================

  /**
   * Draw a train from a snapshot entry (see frontend/src/renderer/render-snapshot.ts).
   *
   * The snapshot's span shape mirrors the controller's: occupied_tiles
   * ordered end_a → end_b, plus end_a / end_b geometry, plus
   * leading_end ∈ {'a','b'}. Per-tile length is not in the network
   * payload for v1; we use a 50cm-per-tile constant for body
   * interpolation (visually approximate but acceptable for v1).
   */
  drawTrainFromSnapshot(train: Train, trainSnap: TrainSnap): void {
    const p = getCanvasPalette();
    const span = trainSnap.span;
    const occupiedTiles = span.occupiedTiles || [];
    if (occupiedTiles.length === 0) return;

    // leading_end is null when the train has no committed travel direction
    // (no live authority); we still draw both endpoints, but as squares
    // rather than a head triangle. It is 'a'/'b' once a head is committed.
    const leadingEnd = span.leadingEnd;
    const hasLeadingEnd = leadingEnd === 'a' || leadingEnd === 'b';

    // Resolve which end is head vs tail. occupiedTiles is ordered
    // end_a → end_b (see network.py). leadingEnd ('a' or 'b') tells us
    // which endpoint is the head.
    const headFromA = leadingEnd === 'a';
    const headEndpoint = headFromA ? span.endA : span.endB;
    const tailEndpoint = headFromA ? span.endB : span.endA;
    const headTile = headEndpoint.tile;
    const tailTile = tailEndpoint.tile;
    const headDistanceCm = headEndpoint.distance_cm;
    const tailDistanceCm = tailEndpoint.distance_cm;
    const headEntryEdge = headEndpoint.entry_edge;
    const tailEntryEdge = tailEndpoint.entry_edge;

    // Per-tile length mirrors `RailwayNetwork._get_tile_length_cm` on the
    // backend: a block tile is `block.length_cm / number_of_tiles_in_block`,
    // and a configured switch is its state-dependent active length
    // (main_length_cm for state 0, diverge_length_cm for state 1). The head
    // physically dwells on a switch for that length, so it must NOT be
    // hardcoded to 0 — doing so pins the head/tail marker at the entry edge
    // and freezes it for the whole transit. Only an unconfigured switch (no
    // entity / 0 lengths) is instant, which correctly reads as a 0-length pin.
    const tracks = this.currentNetwork?.tracks || [];
    const blocks = this.currentNetwork?.blocks || {};
    const switches = this.currentNetwork?.switches || {};
    const DEFAULT_TILE_LENGTH_CM = 50;

    const tileCountByBlock = new Map<string, number>();
    for (const tr of tracks) {
      if (!tr.block_section_id) continue;
      tileCountByBlock.set(
        tr.block_section_id,
        (tileCountByBlock.get(tr.block_section_id) || 0) + 1,
      );
    }

    const getTrack = (tile: [number, number]) => tracks.find((tr) => tr.x === tile[0] && tr.y === tile[1]);
    const getTrackBits = (tile: [number, number]) => getTrack(tile)?.track_bits || 0;
    const getTileLengthCm = (tile: [number, number]) => {
      const t = getTrack(tile);
      if (!t) return DEFAULT_TILE_LENGTH_CM;
      if (t.is_switch) {
        const sw = t.switch_id ? switches[t.switch_id] : undefined;
        if (!sw) return 0;
        return (t.switch_state === 1 ? sw.diverge_length_cm : sw.main_length_cm) || 0;
      }
      if (!t.block_section_id) return DEFAULT_TILE_LENGTH_CM;
      const block = blocks[t.block_section_id];
      const blockLen = block?.length_cm;
      if (!blockLen || blockLen <= 0) return DEFAULT_TILE_LENGTH_CM;
      const tileCount = tileCountByBlock.get(t.block_section_id) || 1;
      return blockLen / tileCount;
    };

    // === Draw train body (white stroke with dark border from tail to head) ===
    // occupied_tiles is ordered end_a → end_b; reverse so the walk goes
    // tail → head (used for the body polyline and the tail exit edge below).
    const tilesTailToHead = headFromA
      ? [...occupiedTiles].reverse()
      : occupiedTiles;
    const occupiedTileCount = occupiedTiles.length;

    // Exit edge from one occupied tile to the next is fixed by their grid
    // positions — derive it directly. Using the live `switch_state` (via
    // getActiveExitEdge) would pick the wrong branch any time the switch state
    // differs from what the train actually traversed.
    const edgeFromDelta = (curr: [number, number], next: [number, number]) => {
      const dx = next[0] - curr[0];
      const dy = next[1] - curr[1];
      if (dy > 0) return 'N';
      if (dy < 0) return 'S';
      if (dx > 0) return 'E';
      if (dx < 0) return 'W';
      return null;
    };
    const isGridAdjacent = (a: [number, number], b: [number, number]) => (
      Math.abs(b[0] - a[0]) + Math.abs(b[1] - a[1]) === 1
    );

    // Calculate head and tail screen positions. The tail's exit edge is
    // grid-derived toward the next occupied tile — the same rule the body
    // polyline uses — so the tail marker never points at a stale switch branch
    // in the train's wake, where live switch_state may have been re-thrown for
    // a following train after this train physically passed. The head's forward
    // exit leaves the occupied span (no next tile), so it falls back to the
    // track-geometry exit inside getTilePathPosition.
    const tailExitEdge = (
      occupiedTileCount > 1 && isGridAdjacent(tilesTailToHead[0], tilesTailToHead[1])
        ? edgeFromDelta(tilesTailToHead[0], tilesTailToHead[1])
        : null
    ) || undefined;

    const headPos = this.getTilePathPosition(
      headTile, headEntryEdge, headDistanceCm,
      getTileLengthCm(headTile), getTrack(headTile),
    );

    const tailPos = this.getTilePathPosition(
      tailTile, tailEntryEdge, tailDistanceCm,
      getTileLengthCm(tailTile), getTrack(tailTile), tailExitEdge,
    );

    const s = this.scale;

    // Tunnel hops break grid adjacency: occupied_tiles can contain a
    // pair of tiles separated by an arbitrary delta. The polyline must
    // not span the tunnel — the body needs to render as two separate
    // pieces, one on each mouth.
    const tunnels = this.currentNetwork?.tunnels || [];
    const findTunnelEdgePair = (a: [number, number], b: [number, number]) => {
      for (const pair of tunnels) {
        if (pair.a.x === a[0] && pair.a.y === a[1] &&
          pair.b.x === b[0] && pair.b.y === b[1]) {
          return { exitEdge: pair.a.edge, entryEdge: pair.b.edge };
        }
        if (pair.b.x === a[0] && pair.b.y === a[1] &&
          pair.a.x === b[0] && pair.a.y === b[1]) {
          return { exitEdge: pair.b.edge, entryEdge: pair.a.edge };
        }
      }
      return null;
    };

    // Build a list of body chunks. Each chunk is a polyline (array of
    // screen points) that will be stroked as one continuous body piece.
    // Tunnel hops start a new chunk so the line doesn't cross the gap.
    const bodyChunks: Point[][] = [];
    let currentChunk: Point[] = [{ x: tailPos.x, y: tailPos.y }];

    const pushEdgePoint = (chunk: Point[], tile: [number, number], edge: string) => {
      const cx = this.gridToScreenX(tile[0], tile[1]);
      const cy = this.gridToScreenY(tile[0], tile[1]);
      const offset = this.getEdgeCenterOffset(edge);
      chunk.push({ x: cx + offset.x, y: cy + offset.y });
    };

    if (occupiedTileCount === 1) {
      // Same tile - direct line
      currentChunk.push({ x: headPos.x, y: headPos.y });
    } else {
      // 2+ tiles - walk tail→head; on each hop pick edges via grid
      // adjacency or tunnel lookup, and split at tunnel hops.
      for (let i = 0; i < tilesTailToHead.length - 1; i++) {
        const currentTile = tilesTailToHead[i];
        const nextTile = tilesTailToHead[i + 1];

        if (isGridAdjacent(currentTile, nextTile)) {
          const exitEdge = edgeFromDelta(currentTile, nextTile);
          if (!exitEdge) continue;
          const entryEdge = this.getOppositeEdge(exitEdge);
          pushEdgePoint(currentChunk, currentTile, exitEdge);
          pushEdgePoint(currentChunk, nextTile, entryEdge);
          continue;
        }

        // Non-grid-adjacent: must be a tunnel hop. End this chunk at
        // currentTile's tunnel mouth, start the next chunk at
        // nextTile's tunnel mouth.
        const pair = findTunnelEdgePair(currentTile, nextTile);
        if (pair) {
          pushEdgePoint(currentChunk, currentTile, pair.exitEdge);
        }
        bodyChunks.push(currentChunk);
        currentChunk = [];
        if (pair) {
          pushEdgePoint(currentChunk, nextTile, pair.entryEdge);
        }
      }
      currentChunk.push({ x: headPos.x, y: headPos.y });
    }
    bodyChunks.push(currentChunk);

    // An unidentified train has no measured length. Its extent is inferred from
    // whichever blocks the detectors report it in, so drawn literally it fills
    // those blocks seam to seam and butts straight into the next one. Pull the
    // drawn body back by one end-marker at each end: the inferred extent then
    // visibly stops short of the seam instead of claiming it, and the retreat
    // clears exactly the space the end squares are placed into below.
    const bodyInset = trainSnap.isUnidentified ? END_MARKER_SIZE * s : 0;
    if (bodyInset > 0) {
      const last = bodyChunks.length - 1;
      bodyChunks[0] = trimPolylineEnd(bodyChunks[0], bodyInset, true);
      bodyChunks[last] = trimPolylineEnd(bodyChunks[last], bodyInset, false);
    }

    // Stroke each chunk as ONE path. Butt caps land at the chunk endpoints
    // (so tunnel-cut ends look clean); round joins bend the body smoothly
    // through zigzag track. Both properties matter to the dashed body of an
    // unidentified train, and both are easy to lose:
    //   - One path, not a stroke per segment. Canvas restarts the dash phase
    //     at every beginPath(), and a chunk carries a vertex at EVERY tile
    //     edge — stroking segment by segment relocks the dash to the tile
    //     grid instead of letting it march along the body.
    //   - Butt caps, not round. A round cap adds lineWidth/2 of ink past both
    //     ends of every dash. The body line is wider than the dash gap, so
    //     round caps close the gaps and the dashed outline fuses back into a
    //     solid, lumpy line — the mark that says "unidentified" disappears.
    const strokeChunk = (
      chunk: Point[],
      strokeStyle: string,
      lineWidth: number,
      dash?: number[],
    ) => {
      if (chunk.length < 2) return;
      this.ctx.save();
      this.ctx.strokeStyle = strokeStyle;
      this.ctx.lineWidth = lineWidth;
      if (dash) this.ctx.setLineDash(dash);
      this.ctx.lineCap = 'butt';
      this.ctx.lineJoin = 'round';

      this.ctx.beginPath();
      this.ctx.moveTo(chunk[0].x, chunk[0].y);
      for (let i = 1; i < chunk.length; i++) {
        this.ctx.lineTo(chunk[i].x, chunk[i].y);
      }
      this.ctx.stroke();

      this.ctx.restore();
    };

    const drawBodySegments = (strokeStyle: string, lineWidth: number, dash?: number[]) => {
      for (const chunk of bodyChunks) {
        strokeChunk(chunk, strokeStyle, lineWidth, dash);
      }
    };

    // Draw dark border then mode-tinted fill (white in ATO/auto, light yellow
    // while manually driven, light purple while the route is being torn down).
    // An unidentified train's outline is dashed and its fill steel: something is on the
    // track that the app is only watching, not driving (design_new.md "Unidentified train
    // trains", Shown as itself). The dash is the mark; the tint alone would read
    // as just another mode.
    const unidentifiedDash = trainSnap.isUnidentified ? [4 * s, 3 * s] : undefined;
    drawBodySegments(p.ink, 5 * s, unidentifiedDash);
    drawBodySegments(trainBodyFill(trainSnap), 3 * s, unidentifiedDash);

    // === Draw end markers ===
    if (hasLeadingEnd) {
      // Cab is active: draw tail square and head triangle
      this.drawTailMarker(tailPos.x, tailPos.y, tailPos.rotation, train);
      this.drawHeadMarker(headPos.x, headPos.y, headPos.rotation);
    } else {
      // No cab active: draw square markers at both ends with end identifiers
      // Calculate rotation pointing from tail to head for consistent body direction
      const dx = headPos.x - tailPos.x;
      const dy = headPos.y - tailPos.y;
      const rotationToHead = Math.atan2(dy, dx);
      const rotationToTail = rotationToHead + Math.PI;
      // drawEndMarker offsets its square forward along whatever rotation it is
      // handed. An unidentified body has just given up one square at each end,
      // so aim each marker INWARD to drop it into that cleared gap; every other
      // train keeps its square just outside the span, where the body still runs
      // to the very end. A square reads identically at r and r+PI, so aiming it
      // the other way moves the marker without turning it.
      const inward = bodyInset > 0;
      this.drawEndMarker(tailPos.x, tailPos.y, inward ? rotationToHead : rotationToTail, train, 'B');
      this.drawEndMarker(headPos.x, headPos.y, inward ? rotationToTail : rotationToHead, train, 'A');
    }

    // === Draw "stuck waiting" indicator (debug overlay) ===
    this._drawStuckIndicator(headPos.x, headPos.y, trainSnap.waitingForBlockId);

    // === Draw presence-unknown badge ===
    if (trainSnap.presenceUnknown) {
      this._drawPresenceUnknownBadge(headPos.x, headPos.y);
    }

    // === Draw label ===
    const headTrackBits = getTrackBits(headTile);
    this.drawTrainLabel(train, headPos.x, headPos.y, headTrackBits, trainSnap.facing, headTile[0], headTile[1], tracks);
  }

  /**
   * Debug overlay ("Show stuck"): when a train is held short of its
   * requested destination because the next resource is occupied/reserved,
   * the dispatcher records that block as `waiting_for_block_id`
   * (see path_dispatcher.extend_path). Draw a dashed orange line with an
   * arrowhead from the train head to the centre of that block so it is
   * obvious at a glance what each stalled train is waiting on.
   *
   * No-op unless the "Show stuck" debug toggle is on and the train has a
   * recorded waiting block whose tiles are present in the current network.
   */
  _drawStuckIndicator(headX: number, headY: number, waitingBlockId: string | null | undefined): void {
    const p = getCanvasPalette();
    if (!this.renderer.debugFlags?.showStuck) return;
    if (!waitingBlockId) return;

    // Resolve the block to the centre of its tiles. The block-section id
    // lives on each track tile (same source renderer-trains uses for
    // per-tile length). A waiting resource that is a switch (not a block)
    // matches no tiles here — we simply draw nothing rather than guess.
    const tracks = this.currentNetwork?.tracks || [];
    let sumX = 0, sumY = 0, n = 0;
    for (const tr of tracks) {
      if (tr.block_section_id !== waitingBlockId) continue;
      sumX += this.gridToScreenX(tr.x, tr.y);
      sumY += this.gridToScreenY(tr.x, tr.y);
      n++;
    }
    if (n === 0) return;
    const targetX = sumX / n;
    const targetY = sumY / n;

    const dx = targetX - headX;
    const dy = targetY - headY;
    const dist = Math.hypot(dx, dy);
    if (dist < 1) return;  // degenerate: train already sits on the block

    const s = this.scale;
    const angle = Math.atan2(dy, dx);
    const ORANGE = p.warning;

    this.ctx.save();

    // Dashed shaft from the train head toward the waited-for block.
    this.ctx.strokeStyle = ORANGE;
    this.ctx.lineWidth = 2 * s;
    this.ctx.lineCap = 'butt';
    this.ctx.setLineDash([5 * s, 4 * s]);
    this.ctx.beginPath();
    this.ctx.moveTo(headX, headY);
    this.ctx.lineTo(targetX, targetY);
    this.ctx.stroke();

    // Solid filled arrowhead at the block end.
    this.ctx.setLineDash([]);
    const headLen = 10 * s;
    const headWidth = 6 * s;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    this.ctx.fillStyle = ORANGE;
    this.ctx.beginPath();
    this.ctx.moveTo(targetX, targetY);
    this.ctx.lineTo(
      targetX - headLen * cos + headWidth * sin,
      targetY - headLen * sin - headWidth * cos,
    );
    this.ctx.lineTo(
      targetX - headLen * cos - headWidth * sin,
      targetY - headLen * sin + headWidth * cos,
    );
    this.ctx.closePath();
    this.ctx.fill();

    this.ctx.restore();
  }

  /**
   * Presence-unknown badge: an amber "?" disc floated above the head marker.
   * Drawn while `presence_unknown` is set on the controller payload — no wired
   * block backs this train's calculated position, so the span may represent
   * nothing physically on the track (design_new.md "Power", Presence unknown).
   * Warning amber, not danger red: it is doubt, not a detected fault.
   */
  _drawPresenceUnknownBadge(headX: number, headY: number): void {
    const p = getCanvasPalette();
    const s = this.scale;
    const r = 7 * s;
    const cx = headX + 10 * s;
    const cy = headY - 12 * s;

    this.ctx.save();
    this.ctx.beginPath();
    this.ctx.arc(cx, cy, r, 0, Math.PI * 2);
    this.ctx.fillStyle = p.warning;
    this.ctx.fill();
    this.ctx.strokeStyle = withAlpha(p.overlayBg, 0.9);
    this.ctx.lineWidth = Math.min(Math.max(1.0, s * 0.35), 2.0);
    this.ctx.stroke();

    this.ctx.fillStyle = p.overlayBg;
    this.ctx.font = `bold ${10 * s}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText('?', cx, cy + 0.5 * s);
    this.ctx.restore();
  }

  /**
   * Draw head marker (white triangle pointing in direction of movement).
   * The triangle's flat (back) edge is aligned with the precise head position.
   */
  drawHeadMarker(x: number, y: number, rotation: number): void {
    const p = getCanvasPalette();
    const s = this.scale;
    const triHeight = 9 * s;  // 1.5x of original 6s
    const triWidth = triHeight * 0.6;  // Width at base

    // Calculate offset to align flat back edge with (x, y)
    // Move forward along the rotation direction by half the triangle height
    const offsetX = Math.cos(rotation) * (triHeight / 2);
    const offsetY = Math.sin(rotation) * (triHeight / 2);

    const drawX = x + offsetX;
    const drawY = y + offsetY;

    this.ctx.save();
    this.ctx.translate(drawX, drawY);
    this.ctx.rotate(rotation);

    this.ctx.fillStyle = p.text;
    this.ctx.beginPath();
    // Tip at front, flat back at -triHeight/2 (centered on x,y after offset)
    this.ctx.moveTo(triHeight / 2, 0);  // Tip
    this.ctx.lineTo(-triHeight / 2, -triWidth);  // Back left
    this.ctx.lineTo(-triHeight / 2, triWidth);   // Back right
    this.ctx.closePath();
    this.ctx.fill();

    this.ctx.strokeStyle = p.ink;
    this.ctx.lineWidth = 1 * s;
    this.ctx.stroke();

    this.ctx.restore();
  }

  /**
   * Draw tail marker (white square with dark border and num_cars display).
   * Mirrors head marker: rotation points forward (toward head), offset forward,
   * so the square's back edge aligns with the tail position (x, y).
   */
  drawTailMarker(x: number, y: number, rotation = 0, train: Train | null = null): void {
    const p = getCanvasPalette();
    const s = this.scale;
    const squareSize = 10 * s;

    // Offset FORWARD (same as head marker) so after drawing centered square,
    // the back edge (-squareSize/2) ends up at the original tail position (x,y)
    const offsetDistance = squareSize / 2;
    const offsetX = Math.cos(rotation) * offsetDistance;
    const offsetY = Math.sin(rotation) * offsetDistance;

    const drawX = x + offsetX;
    const drawY = y + offsetY;

    this.ctx.save();
    this.ctx.translate(drawX, drawY);
    this.ctx.rotate(rotation);  // Rotate square to align with track direction

    // Draw white background square
    this.ctx.fillStyle = p.text;
    this.ctx.fillRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

    // Draw dark border
    this.ctx.strokeStyle = p.ink;
    this.ctx.lineWidth = 1 * s;
    this.ctx.strokeRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

    // Draw num_cars text in center (counter-rotate to keep upright for readability)
    if (train && train.num_cars) {
      this.ctx.save();
      // Counter-rotate both the track rotation AND the square's rotation to keep text upright
      this.ctx.rotate(-rotation);  // Cancel the square's rotation from line 621
      this.ctx.fillStyle = p.ink;
      this.ctx.font = `bold ${6 * s}px sans-serif`;
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'middle';
      this.ctx.fillText(train.num_cars.toString(), 0, 0);
      this.ctx.restore();
    }

    this.ctx.restore();
  }

  /**
   * Draw end marker (white square with dark border and car count + end identifier).
   * Used when no cab is active to label both ends (e.g., "2A", "3B").
   *
   * The square deliberately does NOT name the train: drawTrainLabel already
   * puts the id beside the body on a leader line, so repeating it here only
   * crowds the rail with a second, longer copy of the same fact.
   */
  drawEndMarker(x: number, y: number, rotation = 0, train: Train | null = null, endId = 'A'): void {
    const p = getCanvasPalette();
    const s = this.scale;
    const squareSize = END_MARKER_SIZE * s;

    // Offset forward so the back edge aligns with the position
    const offsetDistance = squareSize / 2;
    const offsetX = Math.cos(rotation) * offsetDistance;
    const offsetY = Math.sin(rotation) * offsetDistance;

    const drawX = x + offsetX;
    const drawY = y + offsetY;

    this.ctx.save();
    this.ctx.translate(drawX, drawY);
    this.ctx.rotate(rotation);

    // Draw white background square
    this.ctx.fillStyle = p.text;
    this.ctx.fillRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

    // Draw dark border
    this.ctx.strokeStyle = p.ink;
    this.ctx.lineWidth = 1 * s;
    this.ctx.strokeRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

    // Draw car number + end identifier (e.g., "2A", "3B")
    if (train && train.num_cars) {
      this.ctx.save();
      // Counter-rotate to keep text upright
      this.ctx.rotate(-rotation);
      this.ctx.fillStyle = p.ink;
      this.ctx.font = `bold ${5 * s}px sans-serif`;
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'middle';
      this.ctx.fillText(`${train.num_cars}${endId}`, 0, 0);
      this.ctx.restore();
    }

    this.ctx.restore();
  }

  /**
   * Draw train label (purple box with train ID).
   * Picks one of 4 quadrants around the train, biased by heading direction
   * and scored against nearby track segments so the label dodges tracks.
   */
  drawTrainLabel(
    train: Train,
    cx: number,
    cy: number,
    trackBits: number,
    direction: string | null,
    tileX: number,
    tileY: number,
    tracks: TrainTrack[],
  ): void {
    const p = getCanvasPalette();
    const s = this.scale;
    const tileSize = this.gridSize * s;

    const labelText = train.id;
    this.ctx.save();
    this.ctx.font = `bold ${9 * s}px sans-serif`;
    const textMetrics = this.ctx.measureText(labelText);
    const textWidth = textMetrics.width;
    const textHeight = 9 * s;
    const padding = 4 * s;
    const boxWidth = textWidth + padding * 2;
    const boxHeight = textHeight + padding * 1.5;
    this.ctx.restore();

    const spacingMultiplier = getLabelSpacingMultiplier();
    const textDependentOffset = Math.SQRT2 * boxWidth;

    const { labelX, labelY } = this._pickLabelPosition(
      train, cx, cy, direction, boxWidth, boxHeight,
      tileSize, spacingMultiplier, textDependentOffset, tracks,
    );

    // Draw leader line
    this.ctx.save();
    this.ctx.strokeStyle = withAlpha(p.text, 0.6);
    this.ctx.lineWidth = 1 * s;
    this.ctx.beginPath();
    this.ctx.moveTo(cx, cy);
    this.ctx.lineTo(labelX, labelY);
    this.ctx.stroke();
    this.ctx.restore();

    // Draw label box — trains are live identity, so they sit one tier
    // above the station label vocabulary: same dark base, but a dim
    // purple fill (~18%) + purple border carry the categorical color
    // without saturating into an alert pill. The motion of the train
    // chassis below is what should read as "live," not the label.
    this.ctx.save();
    this.ctx.font = `bold ${9 * s}px sans-serif`;

    const cornerRadius = 3 * s;
    const boxX = labelX - boxWidth / 2;
    const boxY = labelY - boxHeight / 2;

    this.ctx.beginPath();
    this.ctx.moveTo(boxX + cornerRadius, boxY);
    this.ctx.lineTo(boxX + boxWidth - cornerRadius, boxY);
    this.ctx.quadraticCurveTo(boxX + boxWidth, boxY, boxX + boxWidth, boxY + cornerRadius);
    this.ctx.lineTo(boxX + boxWidth, boxY + boxHeight - cornerRadius);
    this.ctx.quadraticCurveTo(boxX + boxWidth, boxY + boxHeight, boxX + boxWidth - cornerRadius, boxY + boxHeight);
    this.ctx.lineTo(boxX + cornerRadius, boxY + boxHeight);
    this.ctx.quadraticCurveTo(boxX, boxY + boxHeight, boxX, boxY + boxHeight - cornerRadius);
    this.ctx.lineTo(boxX, boxY + cornerRadius);
    this.ctx.quadraticCurveTo(boxX, boxY, boxX + cornerRadius, boxY);
    this.ctx.closePath();

    // Opaque dark base mixed with ~15% purple — the categorical cast is
    // perceptible without saturating into an alert pill, and the label
    // stays legible when it overlaps a station/platform behind it.
    this.ctx.fillStyle = withAlpha(p.overlayBg, 0.92);
    this.ctx.fill();

    this.ctx.strokeStyle = withAlpha(p.accent, 0.9);
    // Cap stroke at ~2px screen width — same screen-pixel cap as platform
    // highlights so canvas-label borders stay hairline at any zoom.
    this.ctx.lineWidth = Math.min(Math.max(1.0, s * 0.35), 2.0);
    this.ctx.stroke();

    this.ctx.fillStyle = p.text;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(labelText, labelX, labelY);
    this.ctx.restore();
  }

  _pickLabelPosition(
    train: Train,
    cx: number,
    cy: number,
    direction: string | null,
    boxW: number,
    boxH: number,
    tileSize: number,
    spacing: number,
    textOffset: number,
    tracks: TrainTrack[],
  ): { labelX: number; labelY: number } {
    const dir = direction || 'EAST';
    const preferred = (typeof DIRECTION_PREFERRED_QUADRANTS !== 'undefined'
      && DIRECTION_PREFERRED_QUADRANTS[dir])
      || ['SE', 'SW', 'NE', 'NW'];
    const quadOffsets: Record<string, { ox: number; oy: number }> = {
      NW: { ox: -0.5, oy: -0.5 },
      NE: { ox: 0.5, oy: -0.5 },
      SW: { ox: -0.5, oy: 0.5 },
      SE: { ox: 0.5, oy: 0.5 },
    };

    const segments = this.renderer.getNearbyTrackSegments(cx, cy, tileSize * 3);
    const platformSegs = this.renderer.platformHandler?.getPlatformBodySegments?.() || [];
    const obstacles = segments.concat(platformSegs);

    // Labels already placed earlier this frame (platforms and stations,
    // plus any earlier trains in the iteration). Used below to penalise
    // candidates that would cover an existing label.
    const labelBounds = this.renderer.labelBounds || [];

    let bestLX = cx, bestLY = cy, bestScore = -Infinity;
    for (let i = 0; i < preferred.length; i++) {
      const { ox, oy } = quadOffsets[preferred[i]] || quadOffsets['SE'];
      const lx = cx + ox * tileSize * spacing + ox * textOffset;
      const ly = cy + oy * tileSize * spacing + oy * textOffset;
      const bx = lx - boxW / 2;
      const by = ly - boxH / 2;

      const dist = this.renderer.distRectToSegments(bx, by, boxW, boxH, obstacles);
      // Saturate so far-from-tracks options become equivalent and the
      // direction preference wins as a tiebreaker.
      const distScore = Math.min(dist, 30);
      const preferenceBonus = (preferred.length - i) * 2;

      // Label-overlap penalty: sum of intersection areas with every
      // previously-placed label, normalised to "fraction of the train
      // label's own area" so the penalty is unit-agnostic. Weight 60
      // is enough that a single full overlap (-60) dominates the max
      // distScore (30) + preferenceBonus (8) combined.
      let labelOverlap = 0;
      for (const lb of labelBounds) {
        labelOverlap += this.renderer.aabbOverlapArea(
          bx, by, boxW, boxH, lb.x, lb.y, lb.w, lb.h,
        );
      }
      const overlapPenalty = (labelOverlap / (boxW * boxH)) * 60;

      const score = distScore + preferenceBonus - overlapPenalty;

      if (score > bestScore) {
        bestScore = score;
        bestLX = lx;
        bestLY = ly;
      }
    }

    // Register this train's chosen rect so the next train placed this
    // frame avoids it too. The cursor-dodge step below may reflect
    // bestLX/bestLY across the train center; we register the picker's
    // chosen position (pre-dodge) because the dodge is a transient,
    // cursor-driven shift, not the label's "natural" home.
    this.renderer.labelBounds.push({
      x: bestLX - boxW / 2,
      y: bestLY - boxH / 2,
      w: boxW,
      h: boxH,
      tier: 'train',
    });

    // Dodge the cursor with hysteresis: only re-evaluate when the mouse
    // actually moves. Otherwise zoom/pan would slide the natural label
    // position under a stationary cursor and flip-flop the dodge state.
    if (!this._labelDodge) this._labelDodge = new Map();
    const mx = this.renderer.mouseX;
    const my = this.renderer.mouseY;
    const prev = this._labelDodge.get(train.id);
    let dodged = prev?.dodged || false;
    if (!prev || prev.mx !== mx || prev.my !== my) {
      if (mx < 0 || my < 0) {
        dodged = false;
      } else {
        const halfW = boxW / 2;
        const halfH = boxH / 2;
        dodged = mx >= bestLX - halfW && mx <= bestLX + halfW
          && my >= bestLY - halfH && my <= bestLY + halfH;
      }
    }
    this._labelDodge.set(train.id, { dodged, mx, my });

    return dodged
      ? { labelX: 2 * cx - bestLX, labelY: 2 * cy - bestLY }
      : { labelX: bestLX, labelY: bestLY };
  }
}
