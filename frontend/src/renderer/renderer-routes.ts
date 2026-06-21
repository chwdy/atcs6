// renderer-routes.ts — route overlays: reachable-tile highlights, per-route
// path fills/borders, direction arrows, switch locks, and the conflict flash.
//
// Migrated from static/js/renderer-routes.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-routes.test.ts.
//
// Part of the Vite bundle; the Renderer constructs it directly
// (`new RendererRouteHandler(this)`).

import type { TileExtentStyle } from './renderer-tracks';

interface RedrawRequest {
  live?: boolean;
  world?: boolean;
  reason?: string;
}

export interface Route {
  route_id?: string;
  status?: string;
  path_tiles?: Array<[number, number]>;
  tile_directions?: Record<string, string>;
  switch_positions?: Record<string, unknown>;
}

/** Visual style for a tile flash. The conflict flash is a red strobe with a
 *  white X; a "locate" flash (e.g. clicking a tile row in the block editor) is
 *  a solid single-color highlight with no mark. */
interface FlashStyle {
  rgb: string; // "r, g, b" channels for the diamond fill
  fillAlpha: number; // diamond fill opacity
  blink: boolean; // true = strobe on/off every 200ms (conflict); false = solid
  mark: 'x' | 'none'; // 'x' = white cross overlay (conflict); 'none' = clean
  // true = also light the track running through each flashed tile (the block
  // editor's "locate" flash), so the highlight shows the rail, not just the cell
  highlightTrack: boolean;
}

const CONFLICT_FLASH_STYLE: FlashStyle = {
  rgb: '232, 63, 91',
  fillAlpha: 0.5,
  blink: true,
  mark: 'x',
  highlightTrack: false,
};

/** The slice of the Renderer the route handler reads through its back-reference. */
export interface RouteHandlerRenderer {
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

export class RendererRouteHandler {
  renderer: RouteHandlerRenderer;
  reachableHighlights: Set<string>;
  activeRoutes: Route[];
  highlightedRouteId: string | null | undefined;
  conflictFlashTiles: Array<[number, number]>;
  conflictFlashEnd: number;
  flashStyle: FlashStyle;

  constructor(renderer: RouteHandlerRenderer) {
    this.renderer = renderer;

    this.reachableHighlights = new Set();
    this.activeRoutes = [];
    this.highlightedRouteId = null;
    this.conflictFlashTiles = [];
    this.conflictFlashEnd = 0;
    this.flashStyle = { ...CONFLICT_FLASH_STYLE };
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
  get currentNetwork(): unknown {
    return this.renderer.currentNetwork;
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

  highlightRoute(route: Route | null | undefined): void {
    this.highlightedRouteId = route ? route.route_id : null;
    if (this.currentNetwork) {
      this.renderer.requestRedraw({ live: true, world: false, reason: 'route-overlay' });
    }
  }

  clearRouteHighlight(): void {
    this.highlightedRouteId = null;
    if (this.currentNetwork) {
      this.renderer.requestRedraw({ live: true, world: false, reason: 'route-overlay' });
    }
  }

  clearRoutePreview(): void {
    this.reachableHighlights = new Set();
  }

  // General tile flash. `flashConflictTiles` is the red-strobe-with-X special
  // case; callers that just want to point at a tile (e.g. the block editor's
  // tile list) pass their own color and a solid, mark-free style.
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
    this.conflictFlashTiles = tiles || [];
    this.conflictFlashEnd = Date.now() + (opts.durationMs ?? 650);

    if (this.conflictFlashTiles.length > 0) {
      this._animateConflictFlash();
    }
  }

  flashConflictTiles(tiles: Array<[number, number]> | null | undefined, durationMs = 2000): void {
    this.flashTiles(tiles, { ...CONFLICT_FLASH_STYLE, durationMs });
  }

  _animateConflictFlash(): void {
    if (Date.now() >= this.conflictFlashEnd) {
      this.conflictFlashTiles = [];
      if (this.currentNetwork) {
        this.renderer.requestRedraw({ live: true, world: false, reason: 'route-overlay' });
      }
      return;
    }

    if (this.currentNetwork) {
      this.renderer.requestRedraw({ live: true, world: false, reason: 'route-overlay' });
    }

    requestAnimationFrame(() => this._animateConflictFlash());
  }

  drawRouteOverlays(): void {
    this.ctx.save();

    if (this.reachableHighlights.size > 0) {
      this.drawReachableHighlights();
    }

    if (this.activeRoutes.length > 0) {
      for (const route of this.activeRoutes) {
        const isHighlighted = route.route_id === this.highlightedRouteId;
        this.drawRoutePath(route, isHighlighted);
      }
    }

    if (this.highlightedRouteId && !this.activeRoutes.some((r) => r.route_id === this.highlightedRouteId)) {
      // Vestigial no-op branch preserved from the classic source (it had no
      // body there either); kept as-is to stay behavior-identical.
    }

    if (Date.now() < this.conflictFlashEnd && this.conflictFlashTiles.length > 0) {
      this.drawConflictFlash();
    }

    this.ctx.restore();
  }

  drawReachableHighlights(): void {
    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;

    this.ctx.fillStyle = 'rgba(255, 157, 0, 0.25)';

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

    this.ctx.strokeStyle = 'rgba(255, 157, 0, 0.6)';
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

  drawRoutePath(route: Route, emphasized = false): void {
    if (!route.path_tiles || route.path_tiles.length === 0) return;

    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;

    const statusColors: Record<string, string> = {
      pending: 'rgba(136, 136, 136, 0.3)',
      setting: 'rgba(255, 217, 61, 0.3)',
      set: 'rgba(4, 211, 97, 0.3)',
      active: 'rgba(0, 255, 136, 0.4)',
      clearing: 'rgba(255, 149, 0, 0.3)',
      completed: 'rgba(102, 102, 102, 0.2)',
      cancelled: 'rgba(232, 63, 91, 0.2)',
    };

    const fillOpacity = emphasized ? 0.4 : 0.2;
    const baseColor = statusColors[route.status ?? ''] || 'rgba(100, 100, 100, 0.2)';

    let fillColor = baseColor;
    if (emphasized) {
      fillColor = baseColor.replace(/[\d.]+\)$/, `${fillOpacity})`);
    }

    this.ctx.fillStyle = fillColor;
    for (const tile of route.path_tiles) {
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

    if (emphasized) {
      const borderColors: Record<string, string> = {
        pending: '#888',
        setting: '#ffd93d',
        set: '#04d361',
        active: '#00ff88',
        clearing: '#ff9500',
        completed: '#666',
        cancelled: '#e83f5b',
      };

      this.ctx.strokeStyle = borderColors[route.status ?? ''] || '#888';
      this.ctx.lineWidth = 2 * this.scale;

      for (const tile of route.path_tiles) {
        const [x, y] = tile;
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

    if (route.tile_directions) {
      this.drawRouteDirectionArrows(route, halfTile);
    }

    if (route.switch_positions && emphasized) {
      this.ctx.fillStyle = '#8B4513';
      for (const key of Object.keys(route.switch_positions)) {
        const [x, y] = key.split(',').map(Number);
        const cx = this.gridToScreenX(x, y);
        const cy = this.gridToScreenY(x, y);

        const lockSize = 6 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(cx, cy - lockSize, lockSize * 0.6, 0, Math.PI * 2);
        this.ctx.fill();
        this.ctx.fillRect(cx - lockSize * 0.5, cy - lockSize * 0.5, lockSize, lockSize);
      }
    }
  }

  drawRouteDirectionArrows(route: Route, halfTile: number): void {
    if (!route.tile_directions || !route.path_tiles) return;

    const arrowColors: Record<string, string> = {
      pending: 'rgba(136, 136, 136, 0.8)',
      setting: 'rgba(255, 217, 61, 0.9)',
      set: 'rgba(4, 211, 97, 0.9)',
      active: 'rgba(0, 255, 136, 0.9)',
      clearing: 'rgba(255, 149, 0, 0.9)',
      completed: 'rgba(102, 102, 102, 0.6)',
      cancelled: 'rgba(232, 63, 91, 0.6)',
    };

    const arrowColor = arrowColors[route.status ?? ''] || 'rgba(100, 100, 100, 0.8)';
    const arrowSize = halfTile * 0.3;

    this.ctx.fillStyle = arrowColor;

    for (const tile of route.path_tiles) {
      const [x, y] = tile;
      const key = `${x},${y}`;
      const direction = route.tile_directions[key];

      if (!direction) continue;

      const cx = this.gridToScreenX(x, y);
      const cy = this.gridToScreenY(x, y);

      this.ctx.beginPath();

      switch (direction) {
        case 'N':
          this.ctx.moveTo(cx - arrowSize * 0.8, cy - arrowSize * 0.4);
          this.ctx.lineTo(cx + arrowSize * 0.4, cy + arrowSize * 0.2);
          this.ctx.lineTo(cx + arrowSize * 0.2, cy - arrowSize * 0.6);
          break;
        case 'S':
          this.ctx.moveTo(cx + arrowSize * 0.8, cy + arrowSize * 0.4);
          this.ctx.lineTo(cx - arrowSize * 0.4, cy - arrowSize * 0.2);
          this.ctx.lineTo(cx - arrowSize * 0.2, cy + arrowSize * 0.6);
          break;
        case 'E':
          this.ctx.moveTo(cx + arrowSize * 0.8, cy - arrowSize * 0.4);
          this.ctx.lineTo(cx - arrowSize * 0.4, cy + arrowSize * 0.2);
          this.ctx.lineTo(cx - arrowSize * 0.2, cy - arrowSize * 0.6);
          break;
        case 'W':
          this.ctx.moveTo(cx - arrowSize * 0.8, cy + arrowSize * 0.4);
          this.ctx.lineTo(cx + arrowSize * 0.4, cy - arrowSize * 0.2);
          this.ctx.lineTo(cx + arrowSize * 0.2, cy + arrowSize * 0.6);
          break;
      }

      this.ctx.closePath();
      this.ctx.fill();
    }
  }

  drawConflictFlash(): void {
    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;
    const style = this.flashStyle;

    // A strobing flash (conflict) hides on the "off" half-cycle; a solid flash
    // (locate) stays drawn for its whole window.
    if (style.blink && Math.floor(Date.now() / 200) % 2 !== 0) return;

    this.ctx.fillStyle = `rgba(${style.rgb}, ${style.fillAlpha})`;

    for (const tile of this.conflictFlashTiles) {
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
      const keys = new Set(this.conflictFlashTiles.map(([x, y]) => `${x},${y}`));
      this.renderer.trackHandler.drawTileExtentHighlight(keys, {
        tileFill: `rgba(${style.rgb}, 0.16)`,
        glow: `rgba(${style.rgb}, 0.5)`,
        core: `rgba(${style.rgb}, 1)`,
        cap: 'rgba(255, 255, 255, 0.95)',
      });
    }

    if (style.mark === 'x') {
      this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
      this.ctx.lineWidth = 3 * this.scale;

      for (const tile of this.conflictFlashTiles) {
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
