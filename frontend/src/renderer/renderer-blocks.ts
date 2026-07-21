// renderer-blocks.ts — the BLOCK (grouping) overlay.
//
// A `Block` is the power + occupancy + reservation unit: it groups one or more
// `Segment`s (the geometry) onto one shared feed. This overlay tints each tile
// by the block that owns it, so the operator can see the grouping at a glance.
// The geometry overlays (per-segment colors, boundary markers, detail labels,
// metrics) live in renderer-segments.ts.
//
// This is the renamed power-district overlay: grouping used to be a separate
// "power district" wrapped around blocks, and is now the block itself.
// Covered by renderer-blocks.test.ts.

import { getActiveSegments, DEBUG_BLOCK_WIDTH } from '../lib/constants';
import { blockColorCss } from '../lib/block-color';
import type { SegmentInfo, SegmentTrack, EdgePoint } from './renderer-segments';
import type { TileExtentStyle } from './renderer-tracks';

export type { EdgePoint };

/**
 * A block as the network payload carries it: power / occupancy / availability
 * state, plus its member segments. The geometry keys (`length_cm`,
 * `min_radius_cm`, `grade_percent`, `speed_limit_kmh`) are the server's derived
 * aggregate reads over the members — per-segment geometry is read from
 * `segments`, never written here.
 */
export interface BlockInfo {
  block_id?: string;
  name?: string;
  state?: string;
  disabled?: boolean;
  manual?: boolean;
  occupied_by?: string | null;
  is_holding_point?: boolean;
  allowed_travel_directions?: string[];
  // Single signed track voltage (-12V..+12V): magnitude is the drive level,
  // sign is polarity. The voltage debug overlay splits it into + / - rails.
  current_voltage?: number;
  // Grouping: the member segment ids, and each member's own geometry.
  segment_ids?: string[];
  segments?: SegmentInfo[];
  // Derived aggregate geometry reads over the member segments.
  length_cm?: number;
  min_radius_cm?: number;
  grade_percent?: number;
  speed_limit_kmh?: number;
}

/** A tile as the block overlay reads it (same payload shape the segment overlay uses). */
export type BlockTrack = SegmentTrack;

/** The slice of the Renderer the block handler reads through its back-reference. */
export interface BlockHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentBlocks?: Record<string, BlockInfo> | null;
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

/** Tint for a tile whose block groups nothing — its own single-segment feed. */
export const UNGROUPED_BLOCK_COLOR = 'hsla(0, 0%, 55%, 0.22)';

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

  gridToScreenX(x: number, y: number): number {
    return this.renderer.gridToScreenX(x, y);
  }
  gridToScreenY(x: number, y: number): number {
    return this.renderer.gridToScreenY(x, y);
  }

  /**
   * Color each tile by the block that owns it. A block that groups several
   * segments gets that block's deterministic color; a block with a single
   * segment is the default "own feed" and reads faint grey, so a real grouping
   * stands out from the ungrouped baseline. Same band geometry as the segment
   * color overlay — only the color source differs.
   *
   * `blocks` defaults to the renderer's cached block map.
   */
  drawBlockGroupingColors(
    tracks: BlockTrack[],
    blocks?: Record<string, BlockInfo> | null,
  ): void {
    const blockMap = blocks === undefined ? this.currentBlocks : blocks;

    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    for (const track of tracks) {
      if (!track.block_section_id) continue;

      const block = blockMap ? blockMap[track.block_section_id] : undefined;
      const memberCount = block?.segment_ids?.length ?? 0;
      const color =
        memberCount > 1 ? blockColorCss(track.block_section_id) : UNGROUPED_BLOCK_COLOR;

      const cx = this.gridToScreenX(track.x, track.y);
      const cy = this.gridToScreenY(track.x, track.y);

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
}
