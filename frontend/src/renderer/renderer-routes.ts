// renderer-routes.ts — route overlays: reachable-tile highlights, per-route
// path fills/borders, direction arrows, switch locks, and the conflict flash.
//
// Migrated from static/js/renderer-routes.js (longterm_todo.md item #7,
// renderer cluster). Behavior is unchanged; covered by renderer-routes.test.ts.
//
// Bundled into static/dist/main.js and attached to window.RendererRouteHandler
// so the still-classic Renderer keeps constructing it until renderer.js migrates.

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

/** The slice of the Renderer the route handler reads through its back-reference. */
export interface RouteHandlerRenderer {
  ctx: CanvasRenderingContext2D;
  scale: number;
  gridSize: number;
  currentNetwork?: unknown;
  gridToScreenX(x: number, y: number): number;
  gridToScreenY(x: number, y: number): number;
  requestRedraw(opts: RedrawRequest): void;
}

export class RendererRouteHandler {
  renderer: RouteHandlerRenderer;
  reachableHighlights: Set<string>;
  activeRoutes: Route[];
  highlightedRouteId: string | null | undefined;
  conflictFlashTiles: Array<[number, number]>;
  conflictFlashEnd: number;

  constructor(renderer: RouteHandlerRenderer) {
    this.renderer = renderer;

    this.reachableHighlights = new Set();
    this.activeRoutes = [];
    this.highlightedRouteId = null;
    this.conflictFlashTiles = [];
    this.conflictFlashEnd = 0;
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

  flashConflictTiles(tiles: Array<[number, number]> | null | undefined, durationMs = 2000): void {
    this.conflictFlashTiles = tiles || [];
    this.conflictFlashEnd = Date.now() + durationMs;

    if (this.conflictFlashTiles.length > 0) {
      this._animateConflictFlash();
    }
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

    const flash = Math.floor(Date.now() / 200) % 2 === 0;
    if (!flash) return;

    this.ctx.fillStyle = 'rgba(232, 63, 91, 0.5)';

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

declare global {
  interface Window {
    RendererRouteHandler?: typeof RendererRouteHandler;
  }
}

// Browser-global bridge so the still-classic Renderer keeps doing
// `new RendererRouteHandler(this)` until renderer.js migrates. Guarded so the
// module also imports cleanly under Node (Vitest), where there is no `window`.
if (typeof window !== 'undefined') {
  window.RendererRouteHandler = RendererRouteHandler;
}
