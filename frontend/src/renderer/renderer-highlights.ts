// renderer-highlights.ts — two Editor highlights drawn on the live layer: the
// soft fill of a reachable tile set (the trace tool) and the timed "locate"
// flash that points at tiles (a segment's tile list, a tunnel mouth). Driven by
// the LayoutHighlighter (editor/layout-highlighter.ts).

import type { TileExtentStyle } from './renderer-tracks';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';

interface RedrawRequest {
  live?: boolean;
  world?: boolean;
  reason?: string;
}

/** Visual style for a tile flash. A "locate" flash (e.g. clicking a tile row
 *  in the segment editor) is a solid single-color highlight with no mark. */
interface FlashStyle {
  rgb: string; // "r, g, b" channels for the diamond fill
  fillAlpha: number; // diamond fill opacity
  blink: boolean; // true = strobe on/off every 200ms ; false = solid
  mark: 'x' | 'none'; // 'x' = white cross overlay; 'none' = clean
  // true = also light the track running through each flashed tile (the block
  // editor's "locate" flash), so the highlight shows the rail, not just the cell
  highlightTrack: boolean;
}

/** The slice of the Renderer the highlight handler reads through its back-reference. */
export interface HighlightHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentNetwork?: unknown;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  requestRedraw(opts: RedrawRequest): void;
  // Used by the "locate" flash to light the track through each flashed tile.
  trackHandler: { drawTileExtentHighlight(keys: Set<string>, style: TileExtentStyle): void };
}

export class RendererHighlightHandler {
  renderer: HighlightHandlerRenderer;
  reachableHighlights: Set<string> = new Set();
  flashTilesList: Array<[number, number]> = [];
  flashEnd = 0;
  flashStyle: FlashStyle = { rgb: '0, 212, 255', fillAlpha: 0.4, blink: false, mark: 'none', highlightTrack: false };

  constructor(renderer: HighlightHandlerRenderer) {
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

  setReachableHighlights(positionsSet: Set<string> | null | undefined): void {
    this.reachableHighlights = positionsSet || new Set();
  }

  flashTiles(
    tiles: Array<[number, number]> | null | undefined,
    opts: {
      rgb?: string;
      fillAlpha?: number;
      durationMs?: number;
      blink?: boolean;
      mark?: 'x' | 'none';
      highlightTrack?: boolean;
    } = {}
  ): void {
    this.flashStyle = {
      rgb: opts.rgb ?? '0, 212, 255',
      fillAlpha: opts.fillAlpha ?? 0.4,
      blink: opts.blink ?? false,
      mark: opts.mark ?? 'none',
      highlightTrack: opts.highlightTrack ?? false,
    };
    this.flashTilesList = tiles || [];
    this.flashEnd = Date.now() + (opts.durationMs ?? 650);
    if (this.flashTilesList.length > 0) this._animateFlash();
  }

  _animateFlash(): void {
    if (Date.now() >= this.flashEnd) {
      this.flashTilesList = [];
      if (this.renderer.currentNetwork) {
        this.renderer.requestRedraw({ live: true, world: false, reason: 'highlight' });
      }
      return;
    }
    if (this.renderer.currentNetwork) {
      this.renderer.requestRedraw({ live: true, world: false, reason: 'highlight' });
    }
    requestAnimationFrame(() => this._animateFlash());
  }

  /** Draw both highlights; called once per live repaint. */
  draw(): void {
    this.ctx.save();
    if (this.reachableHighlights.size > 0) this.drawReachableHighlights();
    if (Date.now() < this.flashEnd && this.flashTilesList.length > 0) this.drawFlash();
    this.ctx.restore();
  }

  drawReachableHighlights(): void {
    const p = getCanvasPalette();
    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;

    this.ctx.fillStyle = withAlpha(p.warning, 0.25);

    for (const key of this.reachableHighlights) {
      const [x, y] = key.split(',').map(Number);
      const cx = this.gridToScreenX(x, y);
      const cy = this.gridToScreenY(x, y);

      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.fill();
    }

    this.ctx.strokeStyle = withAlpha(p.warning, 0.6);
    this.ctx.lineWidth = 2 * this.scale;

    for (const key of this.reachableHighlights) {
      const [x, y] = key.split(',').map(Number);
      const cx = this.gridToScreenX(x, y);
      const cy = this.gridToScreenY(x, y);

      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.stroke();
    }
  }

  drawFlash(): void {
    const p = getCanvasPalette();
    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;
    const style = this.flashStyle;

    // A strobing flash hides on the "off" half-cycle; a solid flash
    // (locate) stays drawn for its whole window.
    if (style.blink && Math.floor(Date.now() / 200) % 2 !== 0) return;

    this.ctx.fillStyle = `rgba(${style.rgb}, ${style.fillAlpha})`;

    for (const tile of this.flashTilesList) {
      const [x, y] = tile;
      const cx = this.gridToScreenX(x, y);
      const cy = this.gridToScreenY(x, y);

      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.fill();
    }

    // Light the track through each flashed tile on top of the diamond, so a
    // "locate" flash shows both the tile boundary and the rail section. Reuses
    // the selection/preview track overlay, tinted to the flash color.
    if (style.highlightTrack) {
      const keys = new Set(this.flashTilesList.map(([x, y]) => `${x},${y}`));
      this.renderer.trackHandler.drawTileExtentHighlight(keys, {
        tileFill: `rgba(${style.rgb}, 0.16)`,
        glow: `rgba(${style.rgb}, 0.5)`,
        core: `rgba(${style.rgb}, 1)`,
        cap: withAlpha(p.text, 0.95),
      });
    }

    if (style.mark === 'x') {
      this.ctx.strokeStyle = withAlpha(p.text, 0.8);
      this.ctx.lineWidth = 3 * this.scale;

      for (const tile of this.flashTilesList) {
        const [x, y] = tile;
        const cx = this.gridToScreenX(x, y);
        const cy = this.gridToScreenY(x, y);
        const size = halfTile * 0.4;

        this.ctx.beginPath();
        this.ctx.moveTo(cx - size, cy - size);
        this.ctx.lineTo(cx + size, cy + size);
        this.ctx.moveTo(cx + size, cy - size);
        this.ctx.lineTo(cx - size, cy + size);
        this.ctx.stroke();
      }
    }
  }
}
