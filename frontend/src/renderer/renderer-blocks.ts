// renderer-blocks.ts — per-block tile coloring, block-boundary markers, and the
// block detail labels (with a hover expansion), plus the block preview path.
//
// Migrated from static/js/renderer-blocks.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-blocks.test.ts.
//
// Imports getActiveSegments, trackBitsToEdges, getLabelSpacingMultiplier,
// TRACK_WIDTH, DEBUG_BLOCK_WIDTH from ../lib/constants and getBlockStateColor
// from ../lib/helpers. Part of the Vite bundle; the Renderer constructs it
// directly (`new RendererBlockHandler(this)`).

import {
  trackBitsToEdges,
  getActiveSegments,
  getLabelSpacingMultiplier,
  TRACK_WIDTH,
  DEBUG_BLOCK_WIDTH,
} from '../lib/constants';
import { getBlockStateColor } from '../lib/helpers';
import type { TileExtentStyle } from './renderer-tracks';

export interface EdgePoint {
  x: number;
  y: number;
}

export interface BlockInfo {
  name?: string;
  state?: string;
  disabled?: boolean;
  manual?: boolean;
  length_cm?: number;
  min_radius_cm?: number;
  grade_percent?: number;
  speed_limit_kmh?: number;
  // Single signed track voltage (-12V..+12V): magnitude is the drive level,
  // sign is polarity. The voltage debug overlay splits it into + / - rails.
  current_voltage?: number;
}

export interface BlockTrack {
  x: number;
  y: number;
  block_section_id?: string | null;
  track_bits?: number;
}

/** The slice of the Renderer the block handler reads through its back-reference. */
export interface BlockHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, BlockInfo> | null;
  blockColorCache: Record<string, number | undefined>;
  blockColorCounter: number;
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

export class RendererBlockHandler {
  renderer: BlockHandlerRenderer;

  constructor(renderer: BlockHandlerRenderer) {
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
  get currentBlocks(): Record<string, BlockInfo> | null | undefined {
    return this.renderer.currentBlocks;
  }
  get blockColorCache(): Record<string, number | undefined> {
    return this.renderer.blockColorCache;
  }
  set blockColorCache(value: Record<string, number | undefined>) {
    this.renderer.blockColorCache = value;
  }
  get blockColorCounter(): number {
    return this.renderer.blockColorCounter;
  }
  set blockColorCounter(value: number) {
    this.renderer.blockColorCounter = value;
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

  getBlockColor(blockId: string | null | undefined): string | null {
    if (!blockId) return null;

    if (this.blockColorCache[blockId] !== undefined) {
      const hue = this.blockColorCache[blockId];
      return `hsla(${hue}, 70%, 50%, 0.6)`;
    }

    const goldenAngle = 137.508;
    const hue = (this.blockColorCounter * goldenAngle) % 360;

    this.blockColorCache[blockId] = hue;
    this.blockColorCounter++;

    return `hsla(${hue}, 70%, 50%, 0.6)`;
  }

  drawBlockIdColors(tracks: BlockTrack[]): void {
    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    for (const track of tracks) {
      if (!track.block_section_id) continue;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);
      const color = this.getBlockColor(track.block_section_id);

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

  drawBlockBoundaryMarkers(tracks: BlockTrack[]): void {
    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    const trackMap: Record<string, BlockTrack> = {};
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
      if (!track.block_section_id) continue;

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

        const neighborBlockId = neighbor.block_section_id;
        if (neighborBlockId === track.block_section_id) continue;

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
          '#666',
        );
        this.ctx.restore();
      }
    }
  }

  // Group tracks by block and resolve each block's middle-tile screen anchor
  // (the tile center, before any per-overlay vertical offset). Shared by the
  // block-name/detail overlay and the block-voltage overlay so both label the
  // same tile of each block. One entry per non-empty block, in track order.
  blockAnchors(tracks: BlockTrack[]): Array<{
    blockId: string;
    blockTrackList: BlockTrack[];
    centerTrack: BlockTrack;
    cx: number;
    cy: number;
  }> {
    const blockTracks: Record<string, BlockTrack[]> = {};
    for (const track of tracks) {
      if (!track.block_section_id) continue;
      (blockTracks[track.block_section_id] ||= []).push(track);
    }
    return Object.entries(blockTracks)
      .filter(([, list]) => list.length > 0)
      .map(([blockId, blockTrackList]) => {
        const centerTrack = blockTrackList[Math.floor(blockTrackList.length / 2)];
        return {
          blockId,
          blockTrackList,
          centerTrack,
          cx: this.gridToScreenX(centerTrack.x, centerTrack.y),
          cy: this.gridToScreenY(centerTrack.x, centerTrack.y),
        };
      });
  }

  drawBlockDetailLabels(tracks: BlockTrack[]): void {
    const anchors = this.blockAnchors(tracks);

    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    let hoveredBlockId: string | null = null;
    let hoveredBlockData: {
      blockTrackList: BlockTrack[];
      centerTrack: BlockTrack;
      cx: number;
      cy: number;
    } | null = null;

    for (const { blockId, blockTrackList, centerTrack, cx, cy } of anchors) {
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
        hoveredBlockId = blockId;
        hoveredBlockData = {
          blockTrackList,
          centerTrack,
          cx, cy,
        };
        break;
      }
    }

    for (const { blockId, cx, cy } of anchors) {
      if (blockId === hoveredBlockId) continue;

      const blockInfo: BlockInfo = this.currentBlocks && this.currentBlocks[blockId]
        ? this.currentBlocks[blockId]
        : {};
      const blockName = blockInfo.name || '';
      const shortId = blockId.length > 10 ? blockId.slice(-6) : blockId;
      const displayName = blockName || shortId;

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

      this.ctx.fillStyle = 'rgba(13, 16, 32, 0.7)';
      this.ctx.strokeStyle = 'rgba(100, 100, 150, 0.4)';
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
      this.ctx.fill();
      this.ctx.stroke();

      this.ctx.fillStyle = 'rgba(221, 221, 221, 0.7)';
      this.ctx.fillText(line1, cx + offsetX + neOffsetX, cy + offsetY + neOffsetY);

      this.ctx.restore();
    }

    if (hoveredBlockId && hoveredBlockData) {
      const { blockTrackList, centerTrack, cx, cy } = hoveredBlockData;
      const blockInfo: BlockInfo = this.currentBlocks && this.currentBlocks[hoveredBlockId]
        ? this.currentBlocks[hoveredBlockId]
        : {};

      this.ctx.save();
      const blockColor = this.getBlockColor(hoveredBlockId)!;
      const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

      for (const track of blockTrackList) {
        const trackCx = this.gridToScreenX(track.x, track.y);
        const trackCy = this.gridToScreenY(track.x, track.y);

        this.ctx.save();
        this.ctx.translate(trackCx, trackCy);

        const trackBits = track.track_bits || 0;
        const segments = getActiveSegments(trackBits);

        this.ctx.strokeStyle = blockColor.replace('0.6', '0.9');
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

      const blockName = blockInfo.name || '';
      const blockState = (blockInfo.state || 'FREE').toUpperCase();
      const stateColor = getBlockStateColor(blockState);

      const shortId = hoveredBlockId.length > 10 ? hoveredBlockId.slice(-6) : hoveredBlockId;
      const displayName = blockName || shortId;

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
      const trackCount = blockTrackList.length;

      const line1 = displayName;
      const line2 = `${blockState} ${trackCount} tiles`;
      const lengthStr = blockInfo.length_cm ? `${blockInfo.length_cm}cm` : '-';
      const radiusStr = blockInfo.min_radius_cm ? `R${blockInfo.min_radius_cm}` : '-';
      const speedStr = blockInfo.speed_limit_kmh ? `${blockInfo.speed_limit_kmh}km/h` : '-';
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

      this.ctx.fillStyle = 'rgba(13, 16, 32, 1.0)';
      this.ctx.strokeStyle = 'rgba(100, 100, 150, 1.0)';
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
      this.ctx.fill();
      this.ctx.stroke();

      let yPos = cy + offsetY + neOffsetY;

      this.ctx.fillStyle = '#ddd';
      this.ctx.fillText(line1, cx + offsetX + neOffsetX, yPos);
      yPos += lineHeight;

      this.ctx.fillStyle = stateColor;
      this.ctx.fillText(line2, cx + offsetX + neOffsetX, yPos);
      yPos += lineHeight;

      this.ctx.fillStyle = '#999';
      this.ctx.fillText(line3, cx + offsetX + neOffsetX, yPos);

      this.ctx.restore();
    }
  }

  /**
   * Debug overlay: one consolidated info box per block. Each enabled metric is
   * a color-coded row inside a single box anchored above the block's middle
   * tile, so turning several on stacks lines in one container rather than
   * scattering a separate pill per metric.
   *
   * Rows (top to bottom, only the ones whose flag is set):
   *  - voltage — the signed `current_voltage` (-12V..+12V) split into a + rail
   *    (red, forward polarity) and a − rail (blue, reverse polarity); 0V leaves
   *    both cold.
   *  - length — `length_cm` in gold.
   *  - radius — `min_radius_cm` in cyan (`R<cm>`).
   *  - speed  — `speed_limit_kmh` in green.
   *
   * A row with a real value is bright; a missing/zero value shows a dim
   * placeholder (`--cm`, `R--`, `--km/h`). The background pill is drawn only
   * when at least one enabled row carries a real value, matching the original
   * voltage overlay's "cold blocks stay as faint floating text" rule.
   */
  drawBlockMetrics(
    tracks: BlockTrack[],
    opts: { voltage?: boolean; length?: boolean; radius?: boolean; speed?: boolean },
  ): void {
    const tileSize = this.gridSize * this.scale;
    const fontSize = Math.max(5, 8 * this.scale);
    const gap = 4 * this.scale;
    const lineHeight = fontSize + 3 * this.scale;

    for (const { blockId, cx, cy: baseCy } of this.blockAnchors(tracks)) {
      const info: BlockInfo = this.currentBlocks && this.currentBlocks[blockId]
        ? this.currentBlocks[blockId]
        : {};

      // Build the rows top-to-bottom. Each row is one or more colored text
      // segments plus a `live` flag (true = real value, drawn bright; false =
      // dim placeholder).
      const rows: Array<{ segments: Array<{ text: string; color: string }>; live: boolean }> = [];

      if (opts.voltage) {
        const v = info.current_voltage || 0;
        const plus = v > 0 ? v : 0;
        const minus = v < 0 ? -v : 0;
        rows.push({
          live: plus > 0 || minus > 0,
          segments: [
            { text: `+${plus.toFixed(1)}`, color: plus > 0 ? '#ff6b6b' : 'rgba(255, 107, 107, 0.3)' },
            { text: `−${minus.toFixed(1)}`, color: minus > 0 ? '#5c9cff' : 'rgba(92, 156, 255, 0.3)' },
          ],
        });
      }
      if (opts.length) {
        const len = info.length_cm;
        const has = len !== undefined && len !== null && len > 0;
        rows.push({ live: has, segments: [{ text: has ? `${len}cm` : '--cm', color: has ? '#f0c060' : 'rgba(240, 192, 96, 0.3)' }] });
      }
      if (opts.radius) {
        const rad = info.min_radius_cm;
        const has = rad !== undefined && rad !== null && rad > 0;
        rows.push({ live: has, segments: [{ text: has ? `R${rad}` : 'R--', color: has ? '#60d0f0' : 'rgba(96, 208, 240, 0.3)' }] });
      }
      if (opts.speed) {
        const spd = info.speed_limit_kmh;
        const has = spd !== undefined && spd !== null && spd > 0;
        rows.push({ live: has, segments: [{ text: has ? `${spd}km/h` : '--km/h', color: has ? '#60f090' : 'rgba(96, 240, 144, 0.3)' }] });
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
      // the lower-right block-name label. topCy is the first row's center.
      const anchorY = baseCy - tileSize * 0.30;
      const topCy = anchorY - (rows.length - 1) * lineHeight / 2;

      if (rows.some((row) => row.live)) {
        const padX = 3 * this.scale;
        const padY = 2 * this.scale;
        this.ctx.fillStyle = 'rgba(13, 16, 32, 0.72)';
        this.ctx.strokeStyle = 'rgba(100, 100, 150, 0.4)';
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

  // Block/platform drag-build preview. Same track-aware overlay as the
  // committed selection (drawBoxSelection) — dim tile, lit track stroke, capped
  // run ends — in purple so an in-progress drag reads as the editor accent, not
  // as a finished amber selection.
  drawBlockPreviewPath(path: Array<{ x: number; y: number }> | null | undefined): void {
    if (!path || path.length === 0) return;
    const keys = new Set(path.map((p) => `${p.x},${p.y}`));
    this.renderer.trackHandler.drawTileExtentHighlight(keys, {
      tileFill: 'rgba(130, 87, 229, 0.16)',
      glow: 'rgba(130, 87, 229, 0.30)',
      core: 'rgba(190, 160, 255, 0.95)',
      cap: 'rgba(255, 255, 255, 0.95)',
    });
  }
}
