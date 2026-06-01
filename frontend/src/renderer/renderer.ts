// renderer.ts — Isometric Railway Renderer (the orchestrator).
//
// Migrated from static/js/renderer.js (longterm_todo.md item #7, the last
// renderer-cluster file). Behavior is unchanged. As a module it imports the
// nine now-migrated handler classes + Camera + buildRenderSnapshot directly,
// owns the two stacked canvases, the rAF-coalesced redraw loop, the
// ResizeObserver, mouse tracking, and the shared label-placement geometry.
//
// app.ts constructs it via `new Renderer('rail-canvas')` (imported directly).
// Imports BIT_TO_EDGES from ../lib/constants.

import { BIT_TO_EDGES } from '../lib/constants';
import { Camera } from '../lib/camera';
import { buildRenderSnapshot } from './render-snapshot';
import { RendererTrackHandler, type Track } from './renderer-tracks';
import { RendererSignalHandler } from './renderer-signals';
import { RendererPlatformHandler, type Platform, type PlatformBound } from './renderer-platforms';
import { RendererStationHandler, type Station } from './renderer-stations';
import { RendererBlockHandler, type BlockInfo } from './renderer-blocks';
import { RendererRouteHandler } from './renderer-routes';
import { RendererTrainHandler, type TrainSnap } from './renderer-trains';
import { RendererTunnelHandler } from './renderer-tunnels';
import { RendererDebugHandler } from './renderer-debug';

const COLORS = {
  BACKGROUND: '#0a0a0c',
  GRID_LINES: '#2a2a34',
  AXIS_LINES: '#4a4a5a',
  ORIGIN_MARKER: '#8257e5',
};

const IsometricGeometry = {
  // Track segment orientations in screen space
  //
  // Edge midpoints (from getEdgePoints):
  //   N: (h/2, -h/2)    E: (h/2, h/2)
  //   S: (-h/2, h/2)    W: (-h/2, -h/2)
  //
  // Segment angles in screen space:
  //   NS: (h/2,-h/2) → (-h/2,h/2)   slope=-1  angle=-45° (or 135°)
  //   EW: (h/2,h/2) → (-h/2,-h/2)   slope=1   angle=+45°
  //   NE: (h/2,-h/2) → (h/2,h/2)    slope=∞   angle=90° (vertical)
  //   NW: (h/2,-h/2) → (-h/2,-h/2)  slope=0   angle=0° (horizontal)
  //   SE: (-h/2,h/2) → (h/2,h/2)    slope=0   angle=0° (horizontal)
  //   SW: (-h/2,h/2) → (-h/2,-h/2)  slope=∞   angle=90° (vertical)

  /**
   * Get the rotation angle for a track segment in screen space.
   */
  getSegmentAngle(e1: string, e2: string): number {
    const segment = [e1, e2].sort().join('');
    const angles: Record<string, number> = {
      NS: -Math.PI / 4,   // -45° (135°)
      EW: Math.PI / 4,    // +45°
      EN: Math.PI / 2,    // 90° (vertical) - NE
      NW: 0,              // 0° (horizontal)
      ES: 0,              // 0° (horizontal) - SE
      SW: Math.PI / 2,    // 90° (vertical)
    };
    return angles[segment] || 0;
  },
};

interface DirectionInfo {
  terminal_tile: number[];
  terminal_heading: string;
}

interface LabelBound {
  x: number;
  y: number;
  w: number;
  h: number;
  tier: string;
}

/** The fields of the server network payload the renderer + handlers read. */
interface NetworkPayload {
  tracks?: Track[];
  blocks?: Record<string, BlockInfo>;
  platforms?: Platform[];
  stations?: Station[];
  switches?: Record<string, { occupied_by?: unknown; train_protection_claims?: unknown[]; locks?: unknown[] } | undefined>;
  tunnels?: Array<{ a: { x: number; y: number; edge: string }; b: { x: number; y: number; edge: string } }>;
  trains?: Array<{ id: string; num_cars?: number }>;
}

interface RequestRedrawOpts {
  world?: boolean;
  live?: boolean;
  reason?: string;
  network?: NetworkPayload;
}

class Renderer {
  liveCanvas: HTMLCanvasElement | null;
  worldCanvas: HTMLCanvasElement | null;
  canvas!: HTMLCanvasElement;
  liveCtx!: CanvasRenderingContext2D;
  worldCtx!: CanvasRenderingContext2D;
  ctx!: CanvasRenderingContext2D;
  camera!: Camera;
  mouseX!: number;
  mouseY!: number;
  platformBounds!: PlatformBound[];
  labelBounds!: LabelBound[];
  showBlockColorsOnly!: boolean;
  currentBlocks!: Record<string, BlockInfo>;
  blockColorCache!: Record<string, number | undefined>;
  blockColorCounter!: number;
  currentNetwork!: NetworkPayload | null;
  boxSelection!: Set<string>;
  boxSelectRect!: { startX: number; startY: number; endX: number; endY: number } | null;
  lassoPath!: Array<{ x: number; y: number }> | null;
  trackHandler!: RendererTrackHandler;
  signalHandler!: RendererSignalHandler;
  platformHandler!: RendererPlatformHandler;
  stationHandler!: RendererStationHandler;
  blockHandler!: RendererBlockHandler;
  routeHandler!: RendererRouteHandler;
  trainHandler!: RendererTrainHandler;
  tunnelHandler!: RendererTunnelHandler;
  debugHandler!: RendererDebugHandler;
  _pendingResizeFrame!: number | null;
  _pendingFlush!: { world: boolean; live: boolean; reason: string } | null;
  _flushFrame!: number | null;
  _lastFlushReason!: string;
  debugFlags!: { showBlocks: boolean; showSignals: boolean; showTunnelLinks: boolean; showStuck?: boolean };
  _resizeObserver?: ResizeObserver;
  // Derived per-network state, set by _updateNetworkState.
  currentPlatforms!: Platform[];
  trackMap!: Record<string, Track>;
  currentSnapshot?: { trains?: Record<string, TrainSnap> };
  _pendingTunnelEndpoint?: { x: number; y: number; edge: string } | null;
  _blockDirectionOverlay?: { forward?: DirectionInfo | null; reverse?: DirectionInfo | null } | null;

  constructor(canvasId: string) {
    // Phase 5: two stacked canvases — world (static map shape) below,
    // live (trains, routes, hover, selection) above. The legacy
    // canvasId (#rail-canvas) is the live canvas because mouse events
    // fire on the topmost element. The world canvas is paired by id.
    this.liveCanvas = document.getElementById(canvasId) as HTMLCanvasElement | null;
    this.worldCanvas = document.getElementById('rail-world-canvas') as HTMLCanvasElement | null;
    if (!this.liveCanvas || !this.worldCanvas) {
      console.error('[Renderer] Required canvas elements not found!');
      return;
    }
    // `this.canvas` historically points to the single canvas. Keep it
    // pointing at the live canvas so existing code (mouse handlers,
    // getBoundingClientRect, dimensions) keeps working without changes.
    this.canvas = this.liveCanvas;
    this.liveCtx = this.liveCanvas.getContext('2d')!;
    this.worldCtx = this.worldCanvas.getContext('2d')!;
    // `this.ctx` is the *active* context — handler files draw to it.
    // _drawWorld and _drawLive switch this before calling handlers so
    // handler code (renderer-tracks.ts, renderer-trains.ts, …) stays
    // ignorant of which layer it's painting to.
    this.ctx = this.worldCtx;

    // Viewport (pan, zoom, projection) lives on Camera so editor / app /
    // handler files all read & write through one canonical object. See
    // frontend/src/lib/camera.ts for the math.
    this.camera = new Camera(this.canvas);

    // Mouse position for hover detection
    this.mouseX = -1;
    this.mouseY = -1;
    this.platformBounds = []; // Array of {bounds, name, abbrev} for hover detection
    // Per-frame registry of label rects on the live canvas, used by the
    // train label picker to avoid overlapping already-placed station /
    // platform labels. See §16 "Overlap" in docs/DESIGN_LANGUAGE.md.
    // Items: { x, y, w, h, tier }. Reset at the top of _drawLive.
    this.labelBounds = [];

    // Block display mode:
    // - false: only show when debug checkbox is checked (full debug info)
    // - true: show block colors only (for block tools)
    this.showBlockColorsOnly = false;

    // Current blocks data for track coloring
    this.currentBlocks = {};

    // Block color cache for golden angle distribution
    // Maps block_id -> assigned hue (ensures consistent colors across renders)
    this.blockColorCache = {};
    this.blockColorCounter = 0;  // Increments for each new block

    // Store current network for redraws
    this.currentNetwork = null;

    // Box-select overlay state (driven by the box-select editor tool)
    this.boxSelection = new Set();   // Set of "x,y" keys for highlighted tracks
    this.boxSelectRect = null;       // {startX, startY, endX, endY} screen coords, or null
    // Lasso-select overlay state (driven by the lasso-select editor tool).
    // Array of {x, y} screen-space points being drawn, or null when idle.
    this.lassoPath = null;

    this.trackHandler = new RendererTrackHandler(this);
    this.signalHandler = new RendererSignalHandler(this);
    this.platformHandler = new RendererPlatformHandler(this);
    this.stationHandler = new RendererStationHandler(this);
    this.blockHandler = new RendererBlockHandler(this);
    this.routeHandler = new RendererRouteHandler(this);
    this.trainHandler = new RendererTrainHandler(this);
    this.tunnelHandler = new RendererTunnelHandler(this);
    this.debugHandler = new RendererDebugHandler(this);

    // Set up mouse tracking for hover effects

    this.canvas.addEventListener('mousemove', (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const prevMouseX = this.mouseX;
      const prevMouseY = this.mouseY;
      this.mouseX = e.clientX - rect.left;
      this.mouseY = e.clientY - rect.top;

      // Redraw if mouse moved and we have a network
      // Need redraw for: block hover, platform name hover, train label dodge.
      // debugFlags is refreshed every flush; reading here is safe even if
      // it lags by at most one frame after a debug-checkbox toggle.
      const showBlocks = this.debugFlags?.showBlocks || false;
      const hasPlatforms = this.currentPlatforms && this.currentPlatforms.length > 0;
      const hasTrains = (this.currentNetwork?.trains?.length ?? 0) > 0;

      if (this.currentNetwork && (prevMouseX !== this.mouseX || prevMouseY !== this.mouseY)) {
        if (showBlocks || this.showBlockColorsOnly || hasPlatforms || hasTrains) {
          this.requestRedraw({ live: true, world: false, reason: 'hover' });
        }
      }
    });
    this.canvas.addEventListener('mouseleave', () => {
      const prevMouseX = this.mouseX;
      const prevMouseY = this.mouseY;
      this.mouseX = -1;
      this.mouseY = -1;

      // Redraw when mouse leaves to clear hover state
      const showBlocks = this.debugFlags?.showBlocks || false;
      const hasPlatforms = this.currentPlatforms && this.currentPlatforms.length > 0;
      const hasTrains = (this.currentNetwork?.trains?.length ?? 0) > 0;

      if (this.currentNetwork && (prevMouseX !== -1 || prevMouseY !== -1)) {
        if (showBlocks || this.showBlockColorsOnly || hasPlatforms || hasTrains) {
          this.requestRedraw({ live: true, world: false, reason: 'mouseleave' });
        }
      }
    });

    // rAF-debounced resize so a stream of ResizeObserver callbacks (e.g.,
    // during a CSS transition on the sidebar) collapse to one resize pass.
    this._pendingResizeFrame = null;
    // rAF-coalesced redraw scheduling. Many synchronous requestRedraw
    // calls in one tick collapse to one paint. _pendingFlush carries
    // the OR of all requested layer flags until the next animation frame.
    this._pendingFlush = null;
    this._flushFrame = null;
    this._lastFlushReason = '';
    // Refreshed once per flush from DOM debug checkboxes; hot paths
    // read this instead of querying the DOM per draw.
    this.debugFlags = { showBlocks: false, showSignals: false, showTunnelLinks: false };
    this.resize();
    window.addEventListener('resize', () => this._scheduleResize());

    // Watch the canvas's parent for size changes that don't fire a window
    // resize — e.g., the secondary sidebar opening/closing changes
    // .canvas-container's clientWidth via flex reflow without dispatching
    // a window resize event. ResizeObserver delivers at most one callback
    // per observed element per frame, so we call resize() directly here
    // (not via _scheduleResize) — that keeps the canvas dimensions in
    // lockstep with the parent during a drag-resize. Going through rAF
    // would lag the canvas by one frame and cause a visible flicker.
    if (typeof ResizeObserver !== 'undefined' && this.canvas.parentElement) {
      this._resizeObserver = new ResizeObserver(() => this.resize());
      this._resizeObserver.observe(this.canvas.parentElement);
    }
  }

  /**
   * Used by `window.resize`, which some browsers fire many times per
   * frame during a window drag. rAF-debounces to one resize per frame.
   * ResizeObserver does NOT go through here — it's already one-per-frame
   * and calling resize() directly avoids a one-frame canvas-size lag.
   */
  _scheduleResize(): void {
    if (this._pendingResizeFrame !== null) return;
    this._pendingResizeFrame = requestAnimationFrame(() => {
      this._pendingResizeFrame = null;
      this.resize();
    });
  }

  resize(): void {
    const parent = this.canvas.parentElement;

    // Get dimensions, with fallbacks
    let width = parent ? parent.clientWidth : window.innerWidth;
    let height = parent ? parent.clientHeight : window.innerHeight;

    // Ensure minimum size
    width = Math.max(width, 100);
    height = Math.max(height, 100);

    // Short-circuit if dimensions haven't actually changed (ResizeObserver
    // can fire spuriously during layout settle).
    if (this.canvas.width === width && this.canvas.height === height) {
      return;
    }

    // Resize BOTH canvases so they stay aligned.
    this.liveCanvas!.width = width;
    this.liveCanvas!.height = height;
    this.worldCanvas!.width = width;
    this.worldCanvas!.height = height;

    // Camera updates centerX / centerY from the new canvas dimensions.
    // It does NOT touch panX / panY — see camera.ts resizeToCanvas for
    // the rationale (preserves the world coord at the visual center).
    this.camera.resizeToCanvas();

    // Paint synchronously rather than going through requestRedraw +
    // rAF. Setting canvas.width above just *cleared* both backing
    // buffers; if we deferred the paint to the next animation frame,
    // the browser would commit a blank canvas in the meantime — a
    // visible flicker on every frame of a drag-resize. Painting here
    // lands the new dimensions and the matching frame in the same
    // browser commit. Any pending rAF flush is cancelled so we don't
    // paint twice.
    if (this._flushFrame !== null) {
      cancelAnimationFrame(this._flushFrame);
      this._flushFrame = null;
    }
    this._pendingFlush = { world: true, live: true, reason: 'resize' };
    this._flushRedraw();
  }

  /**
   * Schedule a redraw on the next animation frame. Synchronous callers
   * in the same frame OR their layer flags together; the actual paint
   * happens once per rAF tick.
   */
  requestRedraw(opts: RequestRedrawOpts = {}): void {
    const { world = true, live = true, reason = '', network } = opts;
    if (network !== undefined) {
      this.currentNetwork = network;
    }
    if (this._pendingFlush === null) {
      this._pendingFlush = { world: false, live: false, reason: '' };
    }
    if (world) this._pendingFlush.world = true;
    if (live) this._pendingFlush.live = true;
    if (reason) this._pendingFlush.reason = reason;

    if (this._flushFrame !== null) return;
    this._flushFrame = requestAnimationFrame(() => this._flushRedraw());
  }

  _flushRedraw(): void {
    this._flushFrame = null;
    const pending = this._pendingFlush;
    this._pendingFlush = null;
    if (pending === null) return;
    this._lastFlushReason = pending.reason || '';
    // Phase 5: honor world/live flags by routing to _drawWorld / _drawLive.
    // World layer only repaints when its flag is set, so hover and other
    // live-only updates skip the static map repaint.
    if (!this.currentNetwork) {
      // No network: blank both canvases with just the grid on world.
      this.ctx = this.worldCtx;
      this.draw();
      this._clearLiveCanvas();
      return;
    }
    this._updateNetworkState(this.currentNetwork);
    // Build the per-frame snapshot once so both layers see the same view
    // of sim state. Renderers read from this instead of reaching into
    // window.stateManager directly (Phase 6). The snapshot's span endpoints
    // are richer at runtime than render-snapshot.ts types them, so we widen
    // to the train-handler's shape here.
    this.currentSnapshot = buildRenderSnapshot(this.currentNetwork, window.stateManager) as unknown as {
      trains?: Record<string, TrainSnap>;
    };
    // Refresh debug-checkbox state once per flush. Hot paths read
    // `this.debugFlags.X` instead of querying the DOM on every draw call.
    this._refreshDebugFlags();
    if (pending.world) this._drawWorld(this.currentNetwork);
    if (pending.live) this._drawLive(this.currentNetwork);
  }

  _refreshDebugFlags(): void {
    const showBlocks = document.getElementById('debug-show-blocks') as HTMLInputElement | null;
    const showSignals = document.getElementById('debug-show-signals') as HTMLInputElement | null;
    const showTunnelLinks = document.getElementById('debug-show-tunnel-links') as HTMLInputElement | null;
    const showStuck = document.getElementById('debug-show-stuck') as HTMLInputElement | null;
    this.debugFlags = {
      showBlocks: showBlocks ? !!showBlocks.checked : false,
      showSignals: showSignals ? !!showSignals.checked : false,
      showTunnelLinks: showTunnelLinks ? !!showTunnelLinks.checked : false,
      showStuck: showStuck ? !!showStuck.checked : false,
    };
  }

  /**
   * Set the derived per-network state (currentBlocks, currentPlatforms,
   * trackMap) so handlers in both world and live passes see consistent
   * data. Called once per flush by _flushRedraw before either layer
   * paints, and by the public drawNetwork(network) entry point.
   */
  _updateNetworkState(network: NetworkPayload | null): boolean {
    if (!network || !network.tracks) {
      this.currentNetwork = null;
      this.currentBlocks = {};
      this.currentPlatforms = [];
      this.trackMap = {};
      return false;
    }
    this.currentNetwork = network;
    this.currentBlocks = network.blocks || {};
    this.currentPlatforms = network.platforms || [];
    this.trackMap = {};
    network.tracks.forEach((t) => {
      this.trackMap[`${t.x},${t.y}`] = t;
    });
    return true;
  }

  _clearLiveCanvas(): void {
    // Live canvas uses clearRect (transparent) so the world canvas
    // shows through. Background fill belongs only on the world canvas.
    this.liveCtx.clearRect(0, 0, this.liveCanvas!.width, this.liveCanvas!.height);
  }

  draw(): void {
    // Basic draw - just clear and draw grid if no network loaded
    this.clear();
    this.drawGrid();
  }

  clear(): void {
    this.ctx.fillStyle = COLORS.BACKGROUND;
    this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
  }

  drawGrid(): void {
    this.ctx.save();

    // 1:1 square diamond tile (rotated 45 degrees)
    const tileSize = this.gridSize * this.scale;
    const halfTile = tileSize / 2;

    // Calculate visible grid range based on current pan and viewport
    const viewWidth = this.canvas.width;
    const viewHeight = this.canvas.height;

    // Convert screen corners to grid coordinates to find visible range
    // Add padding to ensure we cover the entire visible area
    const topLeft = this.screenToGrid(0, 0);
    const topRight = this.screenToGrid(viewWidth, 0);
    const bottomLeft = this.screenToGrid(0, viewHeight);
    const bottomRight = this.screenToGrid(viewWidth, viewHeight);

    // Find the bounding box in grid coordinates
    const minGX = Math.min(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x) - 2;
    const maxGX = Math.max(topLeft.x, topRight.x, bottomLeft.x, bottomRight.x) + 2;
    const minGY = Math.min(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y) - 2;
    const maxGY = Math.max(topLeft.y, topRight.y, bottomLeft.y, bottomRight.y) + 2;

    // Draw diamond grid lines
    this.ctx.strokeStyle = COLORS.GRID_LINES;
    this.ctx.lineWidth = 1;
    this.ctx.beginPath();

    // Draw diagonal lines forming diamonds - only for visible tiles
    for (let gx = minGX; gx <= maxGX; gx++) {
      for (let gy = minGY; gy <= maxGY; gy++) {
        // Get screen position of this grid cell's center
        const centerX = this.gridToScreenX(gx, gy);
        const centerY = this.gridToScreenY(gx, gy);

        // Skip if completely outside viewport (with margin for tile size)
        if (centerX < -tileSize || centerX > viewWidth + tileSize ||
          centerY < -tileSize || centerY > viewHeight + tileSize) {
          continue;
        }

        // Diamond corners (screen positions - Y increases downward on canvas)
        // Named by their VISUAL position on screen
        const top = { x: centerX, y: centerY - halfTile };     // visually at top
        const right = { x: centerX + halfTile, y: centerY };   // visually at right
        const bottom = { x: centerX, y: centerY + halfTile };  // visually at bottom
        const left = { x: centerX - halfTile, y: centerY };    // visually at left

        // Draw diamond outline (clockwise from top)
        this.ctx.moveTo(top.x, top.y);
        this.ctx.lineTo(right.x, right.y);
        this.ctx.lineTo(bottom.x, bottom.y);
        this.ctx.lineTo(left.x, left.y);
        this.ctx.lineTo(top.x, top.y);
      }
    }

    this.ctx.stroke();

    // Draw origin axes through grid POINTS (corners), not tile centers
    // Use original formula without +1 offset for grid point positions
    const gridPointX = (gx: number, gy: number) => this.centerX + this.panX + (gx + gy) * tileSize / 2;
    const gridPointY = (gx: number, gy: number) => this.centerY + this.panY + (gx - gy) * tileSize / 2;

    // Calculate extended range for axes (they should extend beyond visible area)
    const axisRange = Math.max(maxGX - minGX, maxGY - minGY) + 10;

    this.ctx.strokeStyle = COLORS.AXIS_LINES;
    this.ctx.lineWidth = 2;

    // X-axis (Y=0 line) - goes from top-left to bottom-right on screen
    this.ctx.beginPath();
    this.ctx.moveTo(gridPointX(-axisRange, 0), gridPointY(-axisRange, 0));
    this.ctx.lineTo(gridPointX(axisRange, 0), gridPointY(axisRange, 0));
    this.ctx.stroke();

    // Y-axis (X=0 line) - goes from top-right to bottom-left on screen
    this.ctx.beginPath();
    this.ctx.moveTo(gridPointX(0, -axisRange), gridPointY(0, -axisRange));
    this.ctx.lineTo(gridPointX(0, axisRange), gridPointY(0, axisRange));
    this.ctx.stroke();

    // Draw origin marker at grid point (0,0) - the corner where 4 tiles meet
    // Use the original formula without +1 offset to get the corner position
    const originX = this.centerX + this.panX;  // (0+0) * tileSize/2 = 0
    const originY = this.centerY + this.panY;  // (0-0) * tileSize/2 = 0
    this.ctx.fillStyle = COLORS.ORIGIN_MARKER;
    this.ctx.beginPath();
    this.ctx.arc(originX, originY, 4, 0, Math.PI * 2);
    this.ctx.fill();

    this.ctx.restore();
  }

  // Projection and viewport math live on Camera (frontend/src/lib/camera.ts).
  // The thin delegators below let internal Renderer methods and handler
  // getters (e.g. `renderer-trains.ts` get scale, gridToScreenX) keep
  // their existing call sites while the canonical state lives on Camera.
  // Any new code should call `this.camera.<method>` directly.

  get panX(): number { return this.camera.panX; }
  set panX(v: number) { this.camera.panX = v; }
  get panY(): number { return this.camera.panY; }
  set panY(v: number) { this.camera.panY = v; }
  get scale(): number { return this.camera.scale; }
  set scale(v: number) { this.camera.scale = v; }
  get centerX(): number { return this.camera.centerX; }
  get centerY(): number { return this.camera.centerY; }
  get gridSize(): number { return this.camera.gridSize; }

  gridToScreenX(gridX: number, gridY = 0): number { return this.camera.gridToScreenX(gridX, gridY); }
  gridToScreenY(gridX: number, gridY = 0): number { return this.camera.gridToScreenY(gridX, gridY); }
  screenToGridX(screenX: number, screenY: number): number { return this.camera.screenToGridX(screenX, screenY); }
  screenToGridY(screenX: number, screenY: number): number { return this.camera.screenToGridY(screenX, screenY); }
  screenToGrid(screenX: number, screenY: number): { x: number; y: number } { return this.camera.screenToGrid(screenX, screenY); }
  centerOnContent(tracks: ReadonlyArray<{ x: number; y: number }>): void { this.camera.centerOnContent(tracks); }

  /**
   * Public entry point for a full repaint (both layers). Used by callers
   * that need synchronous immediate paint — most code should go through
   * requestRedraw() so paints coalesce on rAF.
   */
  drawNetwork(network: NetworkPayload): void {
    if (!this._updateNetworkState(network)) {
      this.ctx = this.worldCtx;
      this.clear();
      this.drawGrid();
      this._clearLiveCanvas();
      return;
    }
    this._drawWorld(network);
    this._drawLive(network);
  }

  /**
   * Paint the static world layer: background, grid, tracks, blocks,
   * platforms, signals, station labels, debug overlays. Repainted only
   * when the world flag is set (map shape / state changes / viewport).
   */
  _drawWorld(network: NetworkPayload): void {
    this.ctx = this.worldCtx;
    this.clear();
    this.drawGrid();

    const tracks = network.tracks || [];

    // Draw block ID colors BEFORE tracks (so they appear underneath).
    // Show if: debug checkbox is checked OR showBlockColorsOnly is set.
    const showBlockDebug = this.debugFlags?.showBlocks || false;
    if (showBlockDebug || this.showBlockColorsOnly) {
      this.blockHandler.drawBlockIdColors(tracks);
    }

    // Tracks (with state-driven coloring).
    tracks.forEach((track) => {
      this.trackHandler.drawTrack(track, false);
    });

    // Grey dots at block boundaries (gaps between different blocks).
    if (network.blocks) {
      this.blockHandler.drawBlockBoundaryMarkers(tracks);
    }

    // Note: platforms AND stations live on the LIVE layer, not world.
    // Both handlers run an inline hover hit-test (`isHovered = mouseX in
    // bounds`) inside their draw method, so the hover effect needs to
    // refresh on every mousemove — which is a live-only redraw. Keeping
    // them on world would freeze the hover state between world flushes
    // (i.e., until the user drag-pans or the network refreshes).

    // Signal-to-block connection lines (debug).
    const showSignalDebug = this.debugFlags?.showSignals || false;
    if (showSignalDebug) {
      this.signalHandler.drawSignalBlockConnections(tracks);
    }

    // Signals on top of connection lines.
    tracks.forEach((track) => {
      if (track.signals && track.signals.length > 0) {
        const cx = this.gridToScreenX(track.x, track.y);
        const cy = this.gridToScreenY(track.x, track.y);
        const cellSize = this.gridSize * this.scale;
        const half = cellSize / 2;
        this.ctx.save();
        this.ctx.translate(cx, cy);
        for (const signal of track.signals) {
          this.signalHandler.drawSingleSignal(signal, track.track_bits, half);
        }
        this.ctx.restore();
      }
    });

    // Tunnel mouths at paired dead-ends, plus the pending first
    // endpoint indicator when the tunnel tool is mid-pair. Dotted
    // pairing lines are gated behind the Tunnel links debug toggle.
    this.tunnelHandler.drawTunnels(
      network.tunnels,
      this._pendingTunnelEndpoint || null,
      { showLinks: !!this.debugFlags?.showTunnelLinks },
    );

    // Other debug overlays.
    this.debugHandler.drawDebugOverlays(tracks);

    // Block info labels (full debug only).
    if (showBlockDebug && !this.showBlockColorsOnly) {
      this.blockHandler.drawBlockDetailLabels(tracks);
    }
  }

  /**
   * Paint the dynamic live layer: route overlays, trains, station hover
   * highlight, box/lasso selection. Cleared with clearRect (transparent)
   * so the world canvas shows through. Repainted on every flush that has
   * the live flag set — i.e., hover, train movement, route changes.
   */
  _drawLive(network: NetworkPayload): void {
    this.ctx = this.liveCtx;
    this._clearLiveCanvas();

    // Rebuild the label-rect registry every live repaint. Platforms push
    // first, then stations, then trains read the accumulated bounds and
    // push their own — order matches the draw sequence below.
    this.labelBounds = [];

    // Platforms first (bodies + labels). Inline hover hit-test inside
    // drawPlatformLabel needs to re-evaluate on every mousemove, so
    // this stays on live. Also populates this._platformBodySegments
    // which the train handler reads.
    this.platformHandler.drawPlatforms();

    // Station base labels (identity tier). Runs hover hit-test, draws
    // non-hovered labels. drawHoverOverlay then layers the hovered
    // label + platform highlights on top.
    this.stationHandler.drawStations();

    // Route overlays (reachable highlights, active routes, conflict flash).
    this.routeHandler.drawRouteOverlays();

    // Block direction arrows — only while the block editor is open. Drawn
    // above the route wash but below trains: it's editor context, not a
    // live entity, so a moving train paints over it (same liveness z-order
    // as station identity labels).
    this.drawBlockDirectionOverlay();

    // Trains.
    if (network.trains) {
      network.trains.forEach((train) => {
        this.trainHandler.drawTrain(train);
      });
    }

    // Hovered station overlay (full name + platform highlights).
    this.stationHandler.drawHoverOverlay();

    // Box/lasso-select overlay.
    this.drawBoxSelection();
    this.drawBoxSelectRect();
    this.drawLassoPath();
  }

  setBoxSelection(keysSet: Set<string> | null): void {
    this.boxSelection = keysSet || new Set();
  }

  setBoxSelectRect(rect: { startX: number; startY: number; endX: number; endY: number } | null): void {
    this.boxSelectRect = rect;
  }

  setLassoPath(points: Array<{ x: number; y: number }> | null): void {
    this.lassoPath = points;
  }

  setPendingTunnelEndpoint(endpoint: { x: number; y: number; edge: string } | null): void {
    this._pendingTunnelEndpoint = endpoint;
    this.requestRedraw({ world: true, live: true, reason: 'tunnel-pending' });
  }

  drawBoxSelection(): void {
    if (!this.boxSelection || this.boxSelection.size === 0) return;
    const cellSize = this.gridSize * this.scale;
    const halfTile = cellSize / 2;
    this.ctx.save();
    this.ctx.fillStyle = 'rgba(255, 205, 30, 0.30)';
    this.ctx.strokeStyle = 'rgba(255, 165, 0, 0.95)';
    this.ctx.lineWidth = 2 * this.scale;
    for (const key of this.boxSelection) {
      const [xs, ys] = key.split(',');
      const x = Number(xs), y = Number(ys);
      const cx = this.gridToScreenX(x, y);
      const cy = this.gridToScreenY(x, y);
      this.ctx.beginPath();
      this.ctx.moveTo(cx, cy - halfTile);
      this.ctx.lineTo(cx + halfTile, cy);
      this.ctx.lineTo(cx, cy + halfTile);
      this.ctx.lineTo(cx - halfTile, cy);
      this.ctx.closePath();
      this.ctx.fill();
      this.ctx.stroke();
    }
    this.ctx.restore();
  }

  drawBoxSelectRect(): void {
    if (!this.boxSelectRect) return;
    const { startX, startY, endX, endY } = this.boxSelectRect;
    const x = Math.min(startX, endX);
    const y = Math.min(startY, endY);
    const w = Math.abs(endX - startX);
    const h = Math.abs(endY - startY);
    if (w === 0 && h === 0) return;
    this.ctx.save();
    this.ctx.fillStyle = 'rgba(255, 205, 30, 0.10)';
    this.ctx.strokeStyle = 'rgba(255, 165, 0, 0.90)';
    this.ctx.lineWidth = 1.5;
    this.ctx.setLineDash([5, 4]);
    this.ctx.fillRect(x, y, w, h);
    this.ctx.strokeRect(x, y, w, h);
    this.ctx.setLineDash([]);
    this.ctx.restore();
  }

  drawLassoPath(): void {
    const pts = this.lassoPath;
    if (!pts || pts.length < 2) return;
    this.ctx.save();
    // Filled interior of the in-progress polygon, lightly shaded.
    this.ctx.fillStyle = 'rgba(255, 205, 30, 0.10)';
    this.ctx.beginPath();
    this.ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) this.ctx.lineTo(pts[i].x, pts[i].y);
    this.ctx.closePath();
    this.ctx.fill();
    // Outline of the drawn path.
    this.ctx.strokeStyle = 'rgba(255, 165, 0, 0.90)';
    this.ctx.lineWidth = 1.5;
    this.ctx.beginPath();
    this.ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) this.ctx.lineTo(pts[i].x, pts[i].y);
    this.ctx.stroke();
    // Dashed closure line from last point back to the start so the user
    // sees what the polygon will be on release.
    this.ctx.setLineDash([5, 4]);
    this.ctx.beginPath();
    this.ctx.moveTo(pts[pts.length - 1].x, pts[pts.length - 1].y);
    this.ctx.lineTo(pts[0].x, pts[0].y);
    this.ctx.stroke();
    this.ctx.setLineDash([]);
    this.ctx.restore();
  }

  setReachableHighlights(positionsSet: Set<string> | null | undefined): void {
    this.routeHandler.setReachableHighlights(positionsSet);
  }

  clearRoutePreview(): void {
    this.routeHandler.clearRoutePreview();
  }

  flashConflictTiles(tiles: Array<[number, number]> | null | undefined, durationMs = 2000): void {
    this.routeHandler.flashConflictTiles(tiles, durationMs);
  }

  highlightPath(path: { route_id?: string } | null | undefined): void {
    this.routeHandler.highlightRoute(path);
  }

  clearPathHighlight(): void {
    this.routeHandler.clearRouteHighlight();
  }

  /**
   * Show / hide the forward + reverse direction arrows for the block being
   * edited. The block editor pushes the resolved geometry here so the
   * operator sees on the map what "forward" and "reverse" mean physically,
   * instead of decoding `F-(8,7)-E` from the panel.
   */
  setBlockDirectionOverlay(overlay: { forward?: DirectionInfo | null; reverse?: DirectionInfo | null } | null): void {
    this._blockDirectionOverlay = overlay || null;
    this.requestRedraw({ world: false, live: true, reason: 'block-direction-overlay' });
  }

  clearBlockDirectionOverlay(): void {
    if (!this._blockDirectionOverlay) return;
    this._blockDirectionOverlay = null;
    this.requestRedraw({ world: false, live: true, reason: 'block-direction-overlay-clear' });
  }

  drawBlockDirectionOverlay(): void {
    const overlay = this._blockDirectionOverlay;
    if (!overlay) return;
    // forward = success-green (the "enter / proceed" hue, per the block
    // palette), reverse = info-cyan. Two distinct accents so the two arrows
    // never read as one gesture.
    this._drawDirectionArrow(overlay.forward, 'F', '#04d361');
    this._drawDirectionArrow(overlay.reverse, 'R', '#00d4ff');
  }

  /**
   * Draw one labelled direction arrow centered on a terminal tile, pointing
   * out the tile's compass exit edge. Reuses the same edge-point geometry the
   * signal renderer uses (`getEdgePoints`), so the screen vector is correct
   * under the isometric projection at any zoom / pan.
   */
  _drawDirectionArrow(info: DirectionInfo | null | undefined, letter: string, color: string): void {
    if (!info) return;
    const tile = info.terminal_tile;
    const heading = info.terminal_heading;
    if (!Array.isArray(tile) || tile.length !== 2 || !heading) return;

    const cx = this.gridToScreenX(tile[0], tile[1]);
    const cy = this.gridToScreenY(tile[0], tile[1]);
    const half = this.gridSize * this.scale / 2;

    // Screen-space unit vector toward the exit edge.
    const edge = this.trackHandler.getEdgePoints(half)[String(heading).toUpperCase()];
    if (!edge) return;
    const len = Math.hypot(edge.x, edge.y) || 1;
    const ux = edge.x / len;
    const uy = edge.y / len;

    // Shaft from tile center toward the edge; arrowhead at the tip.
    const shaft = half * 0.8;
    const tipX = cx + ux * shaft;
    const tipY = cy + uy * shaft;
    const headLen = Math.max(6, half * 0.35);
    const headW = headLen * 0.6;
    // Perpendicular for the two barbs.
    const px = -uy;
    const py = ux;
    const lineW = Math.min(Math.max(1.5, this.scale * 0.6), 3);

    this.ctx.save();
    this.ctx.strokeStyle = color;
    this.ctx.fillStyle = color;
    this.ctx.lineWidth = lineW;
    this.ctx.lineCap = 'round';
    this.ctx.lineJoin = 'round';

    // Shaft.
    this.ctx.beginPath();
    this.ctx.moveTo(cx, cy);
    this.ctx.lineTo(tipX, tipY);
    this.ctx.stroke();

    // Arrowhead.
    this.ctx.beginPath();
    this.ctx.moveTo(tipX, tipY);
    this.ctx.lineTo(tipX - ux * headLen + px * headW, tipY - uy * headLen + py * headW);
    this.ctx.lineTo(tipX - ux * headLen - px * headW, tipY - uy * headLen - py * headW);
    this.ctx.closePath();
    this.ctx.fill();

    // F / R glyph in an opaque dark pill just past the arrowhead, so it
    // stays legible over tracks and block fills (canvas-label opacity rule).
    const labelX = tipX + ux * headLen * 0.9;
    const labelY = tipY + uy * headLen * 0.9;
    const fontPx = Math.max(8, 9 * this.scale);
    this.ctx.font = `bold ${fontPx}px sans-serif`;
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    const tw = this.ctx.measureText(letter).width;
    const padX = 3 * this.scale;
    const padY = 2 * this.scale;
    const boxW = tw + padX * 2;
    const boxH = fontPx + padY * 2;
    this.ctx.fillStyle = 'rgba(15, 15, 18, 0.9)';
    if (this.ctx.roundRect) {
      this.ctx.beginPath();
      this.ctx.roundRect(labelX - boxW / 2, labelY - boxH / 2, boxW, boxH, 3);
      this.ctx.fill();
    } else {
      this.ctx.fillRect(labelX - boxW / 2, labelY - boxH / 2, boxW, boxH);
    }
    this.ctx.strokeStyle = color;
    this.ctx.lineWidth = Math.min(Math.max(1, this.scale * 0.4), 1.5);
    if (this.ctx.roundRect) {
      this.ctx.beginPath();
      this.ctx.roundRect(labelX - boxW / 2, labelY - boxH / 2, boxW, boxH, 3);
      this.ctx.stroke();
    }
    this.ctx.fillStyle = color;
    this.ctx.fillText(letter, labelX, labelY);
    this.ctx.restore();
  }

  // ========================================================================
  // Label placement helpers (shared by train + platform labels)
  // ========================================================================

  getNearbyTrackSegments(cx: number, cy: number, radius: number): Array<{ x1: number; y1: number; x2: number; y2: number }> {
    const tracks = this.currentNetwork?.tracks;
    if (!tracks || !tracks.length) return [];
    const half = this.gridSize * this.scale / 2;
    const edges: Record<string, { x: number; y: number }> = {
      N: { x: half / 2, y: -half / 2 },
      E: { x: half / 2, y: half / 2 },
      S: { x: -half / 2, y: half / 2 },
      W: { x: -half / 2, y: -half / 2 },
    };
    const cutoff = (radius + half) * (radius + half);
    const segments: Array<{ x1: number; y1: number; x2: number; y2: number }> = [];
    for (const tr of tracks) {
      const tx = this.gridToScreenX(tr.x, tr.y);
      const ty = this.gridToScreenY(tr.x, tr.y);
      const dx = tx - cx, dy = ty - cy;
      if (dx * dx + dy * dy > cutoff) continue;
      const bits = tr.track_bits || 0;
      for (let bit = 0; bit < 6; bit++) {
        if (!(bits & (1 << bit))) continue;
        const [e1, e2] = BIT_TO_EDGES[bit];
        segments.push({
          x1: tx + edges[e1].x, y1: ty + edges[e1].y,
          x2: tx + edges[e2].x, y2: ty + edges[e2].y,
        });
      }
    }
    return segments;
  }

  distPointToSegment(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
    const dx = x2 - x1, dy = y2 - y1;
    const lenSq = dx * dx + dy * dy;
    if (lenSq < 1e-6) return Math.hypot(px - x1, py - y1);
    let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  distRectToSegments(bx: number, by: number, w: number, h: number, segments: unknown[]): number {
    if (!segments || !segments.length) return Infinity;
    const cx = bx + w / 2, cy = by + h / 2;
    const samples = [
      [cx, cy],
      [bx, by], [bx + w, by], [bx, by + h], [bx + w, by + h],
      [cx, by], [cx, by + h], [bx, cy], [bx + w, cy],
    ];
    let minDist = Infinity;
    for (const seg of segments as Array<{ x1: number; y1: number; x2: number; y2: number }>) {
      for (const [px, py] of samples) {
        const d = this.distPointToSegment(px, py, seg.x1, seg.y1, seg.x2, seg.y2);
        if (d < minDist) minDist = d;
      }
    }
    return minDist;
  }

  // Axis-aligned bounding-box intersection area in pixels^2. Used by the
  // train label picker to penalise candidates that overlap previously
  // placed labels (see renderer-trains.ts _pickLabelPosition).
  aabbOverlapArea(ax: number, ay: number, aw: number, ah: number, bx: number, by: number, bw: number, bh: number): number {
    const xo = Math.max(0, Math.min(ax + aw, bx + bw) - Math.max(ax, bx));
    const yo = Math.max(0, Math.min(ay + ah, by + bh) - Math.max(ay, by));
    return xo * yo;
  }
}

export { Renderer, COLORS, IsometricGeometry };
