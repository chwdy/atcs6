// renderer-segments.ts — the SEGMENT (geometry) overlay: per-segment tile
// coloring, segment-boundary markers, the segment detail labels (with a hover
// expansion), the per-segment metrics box, and the segment preview path.
//
// A `Segment` is the geometry unit — the run of tiles you draw and whose
// length / min radius / grade / speed limit you edit. Power, occupancy and
// reservation belong to the owning `Block`, whose grouping overlay lives in
// renderer-blocks.ts. Tiles carry BOTH ids (`segment_id` for geometry,
// `block_section_id` for the owning block), so the two overlays never have to
// guess which axis they are painting.
//
// Covered by renderer-segments.test.ts.

import {
  trackBitsToEdges,
  getActiveSegments,
  getLabelSpacingMultiplier,
  TRACK_WIDTH,
  DEBUG_BLOCK_WIDTH,
} from '../lib/constants';
import { getBlockStateColor } from '../lib/helpers';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';
import type { BlockInfo } from './renderer-blocks';
import type { TileExtentStyle } from './renderer-tracks';

export interface EdgePoint {
  x: number;
  y: number;
}

/**
 * A segment's geometry, as the payload carries it. Purely geometric — there is
 * no state / voltage / occupancy here; those live on the owning `BlockInfo`.
 */
export interface SegmentInfo {
  segment_id?: string;
  name?: string;
  length_cm?: number;
  min_radius_cm?: number;
  grade_percent?: number;
  speed_limit_kmh?: number;
}

/**
 * A tile as the overlays read it. `segment_id` is its geometry membership;
 * `block_section_id` is the id of the owning block (the wire name kept from the
 * payload). For a single-segment block the two are equal — which is precisely
 * why each overlay reads the one that matches its axis.
 */
export interface SegmentTrack {
  x: number;
  y: number;
  segment_id?: string | null;
  block_section_id?: string | null;
  track_bits?: number;
}

/** The slice of the Renderer the segment handler reads through its back-reference. */
export interface SegmentHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentSegments?: Record<string, SegmentInfo> | null;
  currentBlocks?: Record<string, BlockInfo> | null;
  segmentColorCache: Record<string, number | undefined>;
  segmentColorCounter: number;
  mouseX: number;
  mouseY: number;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  trackHandler: {
    getEdgePoints(half: number): Record<string, EdgePoint>;
    drawGridSegment(
      start: EdgePoint,
      end: EdgePoint,
      width: number,
      e1: string,
      e2: string,
      color: string | null,
    ): void;
    drawTileExtentHighlight(keys: Set<string>, style: TileExtentStyle): void;
  };
}

export class RendererSegmentHandler {
  renderer: SegmentHandlerRenderer;

  constructor(renderer: SegmentHandlerRenderer) {
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
  get currentSegments(): Record<string, SegmentInfo> | null | undefined {
    return this.renderer.currentSegments;
  }
  get currentBlocks(): Record<string, BlockInfo> | null | undefined {
    return this.renderer.currentBlocks;
  }
  get segmentColorCache(): Record<string, number | undefined> {
    return this.renderer.segmentColorCache;
  }
  set segmentColorCache(value: Record<string, number | undefined>) {
    this.renderer.segmentColorCache = value;
  }
  get segmentColorCounter(): number {
    return this.renderer.segmentColorCounter;
  }
  set segmentColorCounter(value: number) {
    this.renderer.segmentColorCounter = value;
  }
  get mouseX(): number {
    return this.renderer.mouseX;
  }
  get mouseY(): number {
    return this.renderer.mouseY;
  }

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  getSegmentColor(segmentId: string | null | undefined): string | null {
    if (!segmentId) return null;

    if (this.segmentColorCache[segmentId] !== undefined) {
      const hue = this.segmentColorCache[segmentId];
      return `hsla(${hue}, 70%, 50%, 0.6)`;
    }

    const goldenAngle = 137.508;
    const hue = (this.segmentColorCounter * goldenAngle) % 360;

    this.segmentColorCache[segmentId] = hue;
    this.segmentColorCounter++;

    return `hsla(${hue}, 70%, 50%, 0.6)`;
  }

  drawSegmentIdColors(tracks: SegmentTrack[]): void {
    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    for (const track of tracks) {
      if (!track.segment_id) continue;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);
      const color = this.getSegmentColor(track.segment_id);

      this.ctx.save();
      this.ctx.translate(cx, cy);

      const trackBits = track.track_bits || 0;
      const segments = getActiveSegments(trackBits);
      const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

      for (const [e1, e2] of segments) {
        const start = edgePoints[e1];
        const end = edgePoints[e2];
        if (start && end) {
          this.renderer.trackHandler.drawGridSegment(start, end, DEBUG_BLOCK_WIDTH * this.scale, e1, e2, color);
        }
      }

      this.ctx.restore();
    }
  }

  drawSegmentBoundaryMarkers(tracks: SegmentTrack[]): void {
    const p = getCanvasPalette();
    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    const trackMap: Record<string, SegmentTrack> = {};
    for (const track of tracks) {
      trackMap[`${track.x},${track.y}`] = track;
    }

    const neighborDeltas: Record<string, { dx: number; dy: number }> = {
      N: { dx: 0, dy: 1 },
      E: { dx: 1, dy: 0 },
      S: { dx: 0, dy: -1 },
      W: { dx: -1, dy: 0 },
    };

    for (const track of tracks) {
      if (!track.segment_id) continue;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);
      const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

      const trackBits = track.track_bits || 0;
      const connectedEdges = trackBitsToEdges(trackBits);

      const segments = getActiveSegments(trackBits);

      for (const edge of connectedEdges) {
        const delta = neighborDeltas[edge];
        if (!delta) continue;

        const neighborX = track.x + delta.dx;
        const neighborY = track.y + delta.dy;
        const neighbor = trackMap[`${neighborX},${neighborY}`];

        if (!neighbor) continue;

        const neighborSegmentId = neighbor.segment_id;
        if (neighborSegmentId === track.segment_id) continue;

        const ep = edgePoints[edge];
        if (!ep) continue;

        let segmentPartner: string | null = null;
        for (const [e1, e2] of segments) {
          if (e1 === edge) {
            segmentPartner = e2;
            break;
          } else if (e2 === edge) {
            segmentPartner = e1;
            break;
          }
        }

        if (!segmentPartner) continue;

        const epStart = edgePoints[edge];
        const epEnd = edgePoints[segmentPartner];
        const dx = epEnd.x - epStart.x;
        const dy = epEnd.y - epStart.y;
        const length = Math.sqrt(dx * dx + dy * dy);

        const markerDepth = (TRACK_WIDTH * this.scale) / 2;
        const segmentStart = epStart;
        const segmentEnd = {
          x: epStart.x + (dx / length) * markerDepth,
          y: epStart.y + (dy / length) * markerDepth,
        };

        const opposite_edge = ({ N: 'S', S: 'N', E: 'W', W: 'E' } as Record<string, string>)[edge];
        this.ctx.save();
        this.ctx.translate(cx, cy);
        this.renderer.trackHandler.drawGridSegment(
          segmentStart,
          segmentEnd,
          TRACK_WIDTH * this.scale,
          edge,
          opposite_edge,
          p.track,
        );
        this.ctx.restore();
      }
    }
  }

  // Group tiles by segment and resolve each segment's middle-tile screen anchor
  // (the tile center, before any per-overlay vertical offset). Shared by the
  // segment-name/detail overlay and the segment metrics overlay so both label
  // the same tile of each segment. One entry per non-empty segment, in tile
  // order. Each anchor also carries the owning block id (read off any member
  // tile), so a label can pull block-level state without a second index.
  segmentAnchors(tracks: SegmentTrack[]): Array<{
    segmentId: string;
    blockId: string | null;
    segmentTrackList: SegmentTrack[];
    centerTrack: SegmentTrack;
    cx: number;
    cy: number;
  }> {
    const segmentTracks: Record<string, SegmentTrack[]> = {};
    for (const track of tracks) {
      if (!track.segment_id) continue;
      (segmentTracks[track.segment_id] ||= []).push(track);
    }
    return Object.entries(segmentTracks)
      .filter(([, list]) => list.length > 0)
      .map(([segmentId, segmentTrackList]) => {
        const centerTrack = segmentTrackList[Math.floor(segmentTrackList.length / 2)];
        return {
          segmentId,
          blockId: segmentTrackList[0].block_section_id ?? null,
          segmentTrackList,
          centerTrack,
          cx: this.gridToScreenX(centerTrack.x, centerTrack.y),
          cy: this.gridToScreenY(centerTrack.x, centerTrack.y),
        };
      });
  }

  /** The owning block's payload entry for an anchor, or `{}` when unknown. */
  private _blockOf(blockId: string | null): BlockInfo {
    if (!blockId) return {};
    return (this.currentBlocks && this.currentBlocks[blockId]) || {};
  }

  /** A segment's geometry entry, or `{}` when the payload has none. */
  private _segmentOf(segmentId: string): SegmentInfo {
    return (this.currentSegments && this.currentSegments[segmentId]) || {};
  }

  drawSegmentDetailLabels(tracks: SegmentTrack[]): void {
    const p = getCanvasPalette();
    const anchors = this.segmentAnchors(tracks);

    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    let hoveredSegmentId: string | null = null;
    let hoveredSegmentData: {
      blockId: string | null;
      segmentTrackList: SegmentTrack[];
      centerTrack: SegmentTrack;
      cx: number;
      cy: number;
    } | null = null;

    for (const { segmentId, blockId, segmentTrackList, centerTrack, cx, cy } of anchors) {
      const offsetY = tileSize * 0.35;
      const offsetX = tileSize * 0.3;
      const neOffsetX = 10;
      const neOffsetY = -10;

      const labelX = cx + offsetX + neOffsetX;
      const labelY = cy + offsetY + neOffsetY;
      const labelWidth = 100 * this.scale;
      const labelHeight = 40 * this.scale;

      if (this.mouseX >= labelX - 5 && this.mouseX <= labelX + labelWidth + 5 &&
        this.mouseY >= labelY - 5 && this.mouseY <= labelY + labelHeight + 5) {
        hoveredSegmentId = segmentId;
        hoveredSegmentData = {
          blockId,
          segmentTrackList,
          centerTrack,
          cx, cy,
        };
        break;
      }
    }

    for (const { segmentId, cx, cy } of anchors) {
      if (segmentId === hoveredSegmentId) continue;

      const segmentInfo = this._segmentOf(segmentId);
      const segmentName = segmentInfo.name || '';
      const shortId = segmentId.length > 10 ? segmentId.slice(-6) : segmentId;
      const displayName = segmentName || shortId;

      // Use dynamic spacing based on user preference
      const spacingMultiplier = getLabelSpacingMultiplier();
      const offsetY = tileSize * spacingMultiplier;
      const offsetX = tileSize * (spacingMultiplier - 0.05);
      const neOffsetX = 10;
      const neOffsetY = -10;

      this.ctx.save();

      const fontSize = Math.max(5, 8 * this.scale);
      this.ctx.font = `${fontSize}px sans-serif`;
      this.ctx.textAlign = 'left';
      this.ctx.textBaseline = 'top';
      const lineHeight = fontSize + 2;

      const line1 = displayName;
      const line1Width = this.ctx.measureText(line1).width;

      const padding = 1;
      const boxX = cx + offsetX + neOffsetX - padding;
      const boxY = cy + offsetY + neOffsetY - padding;
      const boxWidth = line1Width + padding * 2;
      const boxHeight = lineHeight + padding * 2;

      this.ctx.fillStyle = withAlpha(p.overlayBg, 0.7);
      this.ctx.strokeStyle = withAlpha(p.overlayBorder, 0.4);
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
      this.ctx.fill();
      this.ctx.stroke();

      this.ctx.fillStyle = withAlpha(p.text, 0.7);
      this.ctx.fillText(line1, cx + offsetX + neOffsetX, cy + offsetY + neOffsetY);

      this.ctx.restore();
    }

    if (hoveredSegmentId && hoveredSegmentData) {
      const { blockId, segmentTrackList, cx, cy } = hoveredSegmentData;
      const segmentInfo = this._segmentOf(hoveredSegmentId);
      // Availability / occupancy is block state, not geometry — read it off the
      // owning block so the hover card still reports what the operator needs.
      const blockInfo = this._blockOf(blockId);

      this.ctx.save();
      const segmentColor = this.getSegmentColor(hoveredSegmentId)!;
      const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

      for (const track of segmentTrackList) {
        const trackCx = this.gridToScreenX(track.x, track.y);
        const trackCy = this.gridToScreenY(track.x, track.y);

        this.ctx.save();
        this.ctx.translate(trackCx, trackCy);

        const trackBits = track.track_bits || 0;
        const segments = getActiveSegments(trackBits);

        this.ctx.strokeStyle = segmentColor.replace('0.6', '0.9');
        this.ctx.lineWidth = 12 * this.scale;
        this.ctx.lineCap = 'butt';

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

        this.ctx.restore();
      }

      this.ctx.restore();

      const segmentName = segmentInfo.name || '';
      const blockState = (blockInfo.state || 'FREE').toUpperCase();
      const stateColor = getBlockStateColor(blockState);

      const shortId = hoveredSegmentId.length > 10 ? hoveredSegmentId.slice(-6) : hoveredSegmentId;
      const displayName = segmentName || shortId;

      // Use dynamic spacing based on user preference (same as non-hovered)
      const spacingMultiplier = getLabelSpacingMultiplier();
      const offsetY = tileSize * spacingMultiplier;
      const offsetX = tileSize * (spacingMultiplier - 0.05);
      const neOffsetX = 10;
      const neOffsetY = -10;

      this.ctx.save();

      const fontSize = Math.max(5, 8 * this.scale);
      this.ctx.font = `${fontSize}px sans-serif`;
      this.ctx.textAlign = 'left';
      this.ctx.textBaseline = 'top';
      const lineHeight = fontSize + 2;
      const trackCount = segmentTrackList.length;

      const line1 = displayName;
      const line2 = `${blockState} ${trackCount} tiles`;
      const lengthStr = segmentInfo.length_cm ? `${segmentInfo.length_cm}cm` : '-';
      const radiusStr = segmentInfo.min_radius_cm ? `R${segmentInfo.min_radius_cm}` : '-';
      const speedStr = segmentInfo.speed_limit_kmh ? `${segmentInfo.speed_limit_kmh}km/h` : '-';
      const line3 = `${lengthStr} ${radiusStr} ${speedStr}`;

      const maxWidth = Math.max(
        this.ctx.measureText(line1).width,
        this.ctx.measureText(line2).width,
        this.ctx.measureText(line3).width,
      );

      const padding = 1;
      const boxX = cx + offsetX + neOffsetX - padding;
      const boxY = cy + offsetY + neOffsetY - padding;
      const boxWidth = maxWidth + padding * 2;
      const boxHeight = lineHeight * 3 + padding * 2;

      this.ctx.fillStyle = p.overlayBg;
      this.ctx.strokeStyle = p.overlayBorder;
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
      this.ctx.fill();
      this.ctx.stroke();

      let yPos = cy + offsetY + neOffsetY;

      this.ctx.fillStyle = p.text;
      this.ctx.fillText(line1, cx + offsetX + neOffsetX, yPos);
      yPos += lineHeight;

      this.ctx.fillStyle = stateColor;
      this.ctx.fillText(line2, cx + offsetX + neOffsetX, yPos);
      yPos += lineHeight;

      this.ctx.fillStyle = p.textDim;
      this.ctx.fillText(line3, cx + offsetX + neOffsetX, yPos);

      this.ctx.restore();
    }
  }

  /**
   * Debug overlay: one consolidated info box per segment. Each enabled metric is
   * a color-coded row inside a single box anchored above the segment's middle
   * tile, so turning several on stacks lines in one container rather than
   * scattering a separate pill per metric.
   *
   * Rows (top to bottom, only the ones whose flag is set):
   *  - voltage — the owning block's signed `current_voltage` (-12V..+12V) split
   *    into a + rail (red, forward polarity) and a − rail (blue, reverse
   *    polarity); 0V leaves both cold. Voltage is a block property: every
   *    segment of a grouped block reads the same feed.
   *  - length — the segment's `length_cm` in gold.
   *  - radius — the segment's `min_radius_cm` in cyan (`R<cm>`).
   *  - speed  — the segment's `speed_limit_kmh` in green.
   *
   * A row with a real value is bright; a missing/zero value shows a dim
   * placeholder (`--cm`, `R--`, `--km/h`). The background pill is drawn only
   * when at least one enabled row carries a real value, matching the original
   * voltage overlay's "cold segments stay as faint floating text" rule.
   */
  drawSegmentMetrics(
    tracks: SegmentTrack[],
    opts: { voltage?: boolean; length?: boolean; radius?: boolean; speed?: boolean },
  ): void {
    const p = getCanvasPalette();
    const tileSize = this.gridSize * this.scale;
    const fontSize = Math.max(5, 8 * this.scale);
    const gap = 4 * this.scale;
    const lineHeight = fontSize + 3 * this.scale;

    for (const { segmentId, blockId, cx, cy: baseCy } of this.segmentAnchors(tracks)) {
      const info = this._segmentOf(segmentId);

      // Build the rows top-to-bottom. Each row is one or more colored text
      // segments plus a `live` flag (true = real value, drawn bright; false =
      // dim placeholder).
      const rows: Array<{ segments: Array<{ text: string; color: string }>; live: boolean }> = [];

      if (opts.voltage) {
        const v = this._blockOf(blockId).current_voltage || 0;
        const plus = v > 0 ? v : 0;
        const minus = v < 0 ? -v : 0;
        rows.push({
          live: plus > 0 || minus > 0,
          segments: [
            { text: `+${plus.toFixed(1)}`, color: plus > 0 ? p.danger : withAlpha(p.danger, 0.3) },
            { text: `−${minus.toFixed(1)}`, color: minus > 0 ? p.info : withAlpha(p.info, 0.3) },
          ],
        });
      }
      if (opts.length) {
        const len = info.length_cm;
        const has = len !== undefined && len !== null && len > 0;
        rows.push({ live: has, segments: [{ text: has ? `${len}cm` : '--cm', color: has ? p.caution : withAlpha(p.caution, 0.3) }] });
      }
      if (opts.radius) {
        const rad = info.min_radius_cm;
        const has = rad !== undefined && rad !== null && rad > 0;
        rows.push({ live: has, segments: [{ text: has ? `R${rad}` : 'R--', color: has ? p.info : withAlpha(p.info, 0.3) }] });
      }
      if (opts.speed) {
        const spd = info.speed_limit_kmh;
        const has = spd !== undefined && spd !== null && spd > 0;
        rows.push({ live: has, segments: [{ text: has ? `${spd}km/h` : '--km/h', color: has ? p.success : withAlpha(p.success, 0.3) }] });
      }

      if (rows.length === 0) continue;

      this.ctx.save();
      this.ctx.font = `${fontSize}px monospace`;
      this.ctx.textAlign = 'left';
      this.ctx.textBaseline = 'middle';

      // Lay each row out left-to-right; row width = sum of segment widths plus
      // an inter-segment gap. The box is as wide as the widest row.
      const rowWidths = rows.map((row) =>
        row.segments.reduce((w, seg, i) => w + this.ctx.measureText(seg.text).width + (i > 0 ? gap : 0), 0),
      );
      const boxWidth = Math.max(...rowWidths);
      const textBlockHeight = (rows.length - 1) * lineHeight + fontSize;

      // Center the stack on the tile and sit it above the tile center, clear of
      // the lower-right segment-name label. topCy is the first row's center.
      const anchorY = baseCy - tileSize * 0.30;
      const topCy = anchorY - (rows.length - 1) * lineHeight / 2;

      if (rows.some((row) => row.live)) {
        const padX = 3 * this.scale;
        const padY = 2 * this.scale;
        this.ctx.fillStyle = withAlpha(p.overlayBg, 0.72);
        this.ctx.strokeStyle = withAlpha(p.overlayBorder, 0.4);
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.roundRect(
          cx - boxWidth / 2 - padX,
          topCy - fontSize / 2 - padY,
          boxWidth + padX * 2,
          textBlockHeight + padY * 2,
          3,
        );
        this.ctx.fill();
        this.ctx.stroke();
      }

      rows.forEach((row, i) => {
        let x = cx - rowWidths[i] / 2;
        const y = topCy + i * lineHeight;
        for (const seg of row.segments) {
          this.ctx.fillStyle = seg.color;
          this.ctx.fillText(seg.text, x, y);
          x += this.ctx.measureText(seg.text).width + gap;
        }
      });

      this.ctx.restore();
    }
  }

  // Segment drag-build preview. Same track-aware overlay as the
  // committed selection (drawBoxSelection) — dim tile, lit track stroke, capped
  // run ends — in purple so an in-progress drag reads as the editor accent, not
  // as a finished amber selection.
  drawSegmentPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;
    const p = getCanvasPalette();
    const keys = new Set(path.map((pt) => `${pt.x},${pt.y}`));
    this.renderer.trackHandler.drawTileExtentHighlight(keys, {
      tileFill: withAlpha(p.accent, 0.16),
      glow: withAlpha(p.accent, 0.3),
      core: withAlpha(p.accentBright, 0.95),
      cap: withAlpha(p.text, 0.95),
    });
  }
}
