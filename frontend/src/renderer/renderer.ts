// renderer.ts — Isometric Railway Renderer (the orchestrator).
//
// Migrated from static/js/renderer.js (longterm_todo.md item #7, the last
// renderer-cluster file). As a module it imports the nine handler classes +
// Camera directly, owns the two stacked canvases, the rAF-coalesced redraw
// loop, the ResizeObserver, mouse tracking, and the shared label-placement
// geometry. It draws only the layout; the trains are drawn by a registered
// overlay (see renderer-overlay and running-train-overlay), so a renderer with
// no overlay is a pure layout viewer.
//
// app.ts constructs it via `new Renderer('rail-canvas')` (imported directly).
// Imports BIT_TO_EDGES from ../lib/constants.

import { BIT_TO_EDGES, getActiveSegments, getOppositeEdge, TRACK_WIDTH } from '../lib/constants';
import { Camera } from '../lib/camera';
import { RendererTrackHandler, type Track } from './renderer-tracks';
import { RendererBlockHandler, type BlockInfo } from './renderer-blocks';
import { RendererSegmentHandler, type SegmentInfo } from './renderer-segments';
import { RendererTunnelHandler, type TunnelPair } from './renderer-tunnels';
import { RendererDebugHandler } from './renderer-debug';
import { RendererHighlightHandler } from './renderer-highlights';
import { segmentsFromBlocks, type NetworkPayload } from './payload-types';
import type { OverlayPhase, RendererOverlay } from './renderer-overlay';
import { getCanvasPalette, withAlpha } from '../lib/canvas-palette';
import { gridPreference } from '../lib/grid-preference';
import { canvasLayers, type LayerId } from '../lib/canvas-layers';

// Compass heading of one grid step. Matches block.py `_compass_from_delta`
// (Y positive is north): a +Y step reads N, +X reads E.
export function compassFromDelta(dx: number, dy: number): string | null {
  if (dx === 0 && dy > 0) return 'N';
  if (dx === 0 && dy < 0) return 'S';
  if (dy === 0 && dx > 0) return 'E';
  if (dy === 0 && dx < 0) return 'W';
  return null;
}

// Edges a tile's track actually connects (from its active segments).
function trackEdgeSet(trackBits: number): Set<string> {
  const set = new Set<string>();
  for (const [a, b] of getActiveSegments(trackBits || 0)) {
    set.add(a);
    set.add(b);
  }
  return set;
}

// The other end of the track segment that `edge` belongs to. `preferDir` breaks
// ties on switch tiles where `edge` partners with more than one edge.
function segmentPartner(trackBits: number, edge: string, preferDir: string | null): string | null {
  const partners: string[] = [];
  for (const [a, b] of getActiveSegments(trackBits || 0)) {
    if (a === edge) partners.push(b);
    else if (b === edge) partners.push(a);
  }
  if (partners.length === 0) return null;
  if (preferDir && partners.includes(preferDir)) return preferDir;
  return partners[0];
}

// The [entry, exit] track edges for a forward arrow across one block tile.
// Direction comes from the tile's place in `ordered_tiles` (the step to the
// next tile, or from the previous one), and both edges are taken from the
// tile's own track geometry — so the arrow always lies on the track even when a
// block's stored first-tile `entry_edge` is wrong. (Known data quirk:
// block_geometry_manager._external_entry_edge only derives entry_edge for the
// first tile and can name an edge the tile doesn't have, or "".) `arrivalDir`
// is the step in from the previous ordered tile, `departDir` the step to the next.
export function forwardArrowEdges(
  trackBits: number,
  storedEntry: string,
  arrivalDir: string | null,
  departDir: string | null
): [string, string] | null {
  const edges = trackEdgeSet(trackBits);
  const fromPrev = arrivalDir ? getOppositeEdge(arrivalDir) : null;

  let exitEdge: string | null = null;
  if (departDir && edges.has(departDir)) {
    // Interior / first tile: forward leaves toward the next ordered tile.
    exitEdge = departDir;
  } else {
    // Last / single / non-adjacent tile: leave through the partner of the entry
    // side — from the previous tile when there is one, else the stored entry.
    let entrySide: string | null = null;
    if (fromPrev && edges.has(fromPrev)) entrySide = fromPrev;
    else if (storedEntry && edges.has(storedEntry)) entrySide = storedEntry;
    if (entrySide) exitEdge = segmentPartner(trackBits, entrySide, null);
    else {
      const seg = getActiveSegments(trackBits || 0)[0];
      exitEdge = seg ? seg[1] : null;
    }
  }
  if (!exitEdge) return null;

  const entryEdge = segmentPartner(
    trackBits,
    exitEdge,
    fromPrev && edges.has(fromPrev) ? fromPrev : null
  );
  if (!entryEdge) return null;
  return [entryEdge, exitEdge];
}

// Where to draw a block's forward / reverse terminal arrow, pointing OUT of the
// block at that end. `end` is 'forward' (last ordered tile) or 'reverse' (first
// ordered tile). The exit edge is the terminal tile's track edge facing outside
// the block; entry is its segment partner — all from track geometry, so the
// arrow lies on the track like the per-tile ones.
export function blockTerminalArrowSpec(
  orderedTiles: Array<[number, number, string]>,
  trackMap: Record<string, any>,
  end: 'forward' | 'reverse'
): { x: number; y: number; entryEdge: string; exitEdge: string } | null {
  const n = orderedTiles.length;
  if (n === 0) return null;
  const tileIdx = end === 'forward' ? n - 1 : 0;
  const x = orderedTiles[tileIdx][0];
  const y = orderedTiles[tileIdx][1];
  const track = trackMap[`${x},${y}`];
  if (!track) return null;
  const bits = track.track_bits || 0;

  if (n > 1) {
    // Multi-tile: the in-block neighbour gives the internal edge; the terminal
    // exits through that edge's track partner.
    const nbrIdx = end === 'forward' ? n - 2 : 1;
    const internalEdge = compassFromDelta(
      orderedTiles[nbrIdx][0] - x,
      orderedTiles[nbrIdx][1] - y
    );
    if (!internalEdge) return null;
    const exitEdge = segmentPartner(bits, internalEdge, null);
    return exitEdge ? { x, y, entryEdge: internalEdge, exitEdge } : null;
  }

  // Single-tile block: anchor on the (corrected) stored entry edge. Reverse
  // exits through the entry edge; forward through its partner.
  const edgesSet = trackEdgeSet(bits);
  const stored = String(orderedTiles[0][2] || '').toUpperCase();
  const entry = edgesSet.has(stored) ? stored : Array.from(edgesSet)[0];
  if (!entry) return null;
  const other = segmentPartner(bits, entry, null);
  if (!other) return null;
  return end === 'reverse'
    ? { x, y, entryEdge: other, exitEdge: entry }
    : { x, y, entryEdge: entry, exitEdge: other };
}

interface LabelBound {
  x: number;
  y: number;
  w: number;
  h: number;
  tier: string;
}

// The network payload type (`NetworkPayload`) and its layout/sim split live in
// ./payload-types and are imported above.

interface RequestRedrawOpts {
  world?: boolean;
  live?: boolean;
  reason?: string;
  network?: NetworkPayload;
}

/**
 * The editor's drag-build preview overlay (the "potential tracks" ghost while
 * a track/eraser/block drag is in progress). Pushed by the editor via
 * setBuildPreview and painted in _drawLive — same idiom as box/lasso/tunnel
 * overlays. Drawing it synchronously after requestRedraw() no longer works:
 * the paint is deferred to the next rAF flush, which clears the live canvas
 * first and would wipe an overlay drawn ahead of it.
 */
type BuildPreview =
  | { kind: 'tracks'; tracks: Track[] }
  | { kind: 'eraser'; path: Array<{ x: number; y: number }> }
  | { kind: 'segment'; path: Array<{ x: number; y: number }> }
  | { kind: 'path'; path: Array<{ x: number; y: number }> }
  | { kind: 'invalid'; path: Array<{ x: number; y: number }> };

// One entity the committed selection touches (a block or a switch), carrying only that entity's selected tiles plus how to stroke its
// perimeter. The editor builds these from the network so the renderer can trace
// one coloured outline per entity instead of a field of per-tile cells.
interface SelectionOutlineGroup {
  keys: Set<string>; // "x,y" tiles of this entity that are in the selection
  color: string; // stroke hue (matches the selection-panel chip for this type)
  width: number; // base stroke width in px (scaled by zoom at draw time)
  dash: number[]; // line-dash pattern in px ([] = solid); nests overlapping outlines
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
  labelBounds!: LabelBound[];
  showSegmentColorsOnly!: boolean;
  // When set, the world layer tints each tile by its owning block (the grouping)
  // instead of the per-segment geometry colors. Driven by the block-grouping
  // tool / panel.
  showBlockGroupingColors!: boolean;
  currentBlocks!: Record<string, BlockInfo>;
  currentSegments!: Record<string, SegmentInfo>;
  segmentColorCache!: Record<string, number | undefined>;
  segmentColorCounter!: number;
  currentNetwork!: NetworkPayload | null;
  boxSelection!: Set<string>;
  selectionGroups!: SelectionOutlineGroup[];
  // Per-entity outline groups for the inspect "peek" highlight — a single entity
  // pointed at from a panel cross-link (the switch editor's flank / train-
  // protection links). Drawn with the same dashed rail as the committed
  // selection outline but with no "selected tile" amber fill, and independent of
  // the box-select state, so a peek never reads as a drag-selection.
  inspectHighlightGroups!: SelectionOutlineGroup[];
  boxSelectRect!: { startX: number; startY: number; endX: number; endY: number } | null;
  lassoPath!: Array<{ x: number; y: number }> | null;
  buildPreview!: BuildPreview | null;
  trackHandler!: RendererTrackHandler;
  blockHandler!: RendererBlockHandler;
  segmentHandler!: RendererSegmentHandler;
  highlightHandler!: RendererHighlightHandler;
  tunnelHandler!: RendererTunnelHandler;
  debugHandler!: RendererDebugHandler;
  // Overlays drawn on top of the layout (e.g. the sim view). Empty = pure
  // layout viewer. Populated by the host via registerOverlay().
  _overlays!: RendererOverlay[];
  _pendingResizeFrame!: number | null;
  _pendingFlush!: { world: boolean; live: boolean; reason: string } | null;
  _flushFrame!: number | null;
  debugFlags!: { showBlocks: boolean; showTunnelLinks: boolean; showSwitches?: boolean; showTileDirections?: boolean; showBlockDirections?: boolean; showOneWayDirections?: boolean; showBlockVoltage?: boolean; showBlockLength?: boolean; showBlockRadius?: boolean; showBlockSpeedLimit?: boolean };
  _resizeObserver?: ResizeObserver;
  // Derived per-network state, set by _updateNetworkState.
  trackMap!: Record<string, Track>;
  _pendingTunnelEndpoint?: { x: number; y: number; edge: string } | null;
  // The tunnel pair selected with the Inspect tool, drawn highlighted (wider,
  // brighter stud) with its connecting line. Set via setSelectedTunnel().
  selectedTunnel?: TunnelPair | null;
  _blockDirectionFocusId?: string | null;

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
    // Per-frame registry of label rects on the live canvas, used by the
    // train label picker to avoid overlapping labels already placed. See §16 "Overlap" in docs/DESIGN_LANGUAGE.md.
    // Items: { x, y, w, h, tier }. Reset at the top of _drawLive.
    this.labelBounds = [];

    // Segment display mode:
    // - false: only show when debug checkbox is checked (full debug info)
    // - true: show segment colors only (for segment tools)
    this.showSegmentColorsOnly = false;
    // Block-grouping color overlay (on while a block tool/panel is open).
    this.showBlockGroupingColors = false;

    // Current blocks data (power / occupancy / grouping) for track coloring
    this.currentBlocks = {};
    // Current segment geometry, flattened out of each block's members.
    this.currentSegments = {};

    // Segment color cache for golden angle distribution
    // Maps segment_id -> assigned hue (ensures consistent colors across renders)
    this.segmentColorCache = {};
    this.segmentColorCounter = 0;  // Increments for each new segment

    // Store current network for redraws
    this.currentNetwork = null;

    // Box-select overlay state (driven by the box-select editor tool)
    this.boxSelection = new Set();   // Set of "x,y" keys for highlighted tracks
    // Per-entity outline groups for the committed selection (block / switch
    // footprints), supplied by the editor alongside boxSelection.
    this.selectionGroups = [];
    this.inspectHighlightGroups = [];   // peek highlight from a panel cross-link
    this.boxSelectRect = null;       // {startX, startY, endX, endY} screen coords, or null
    // Lasso-select overlay state (driven by the lasso-select editor tool).
    // Array of {x, y} screen-space points being drawn, or null when idle.
    this.lassoPath = null;
    // Editor drag-build preview ("potential tracks" ghost). Driven by the
    // editor via setBuildPreview; null when no preview is active. Painted in _drawLive (see BuildPreview).
    this.buildPreview = null;

    this.trackHandler = new RendererTrackHandler(this);
    this.blockHandler = new RendererBlockHandler(this);
    this.segmentHandler = new RendererSegmentHandler(this);
    this.highlightHandler = new RendererHighlightHandler(this);
    this.tunnelHandler = new RendererTunnelHandler(this);
    this.debugHandler = new RendererDebugHandler(this);
    this._overlays = [];

    // Set up mouse tracking for hover effects

    this.canvas.addEventListener('mousemove', (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const prevMouseX = this.mouseX;
      const prevMouseY = this.mouseY;
      this.mouseX = e.clientX - rect.left;
      this.mouseY = e.clientY - rect.top;

      // Redraw if mouse moved and we have a network
      // Need redraw for block hover. debugFlags is refreshed every flush;
      // reading here is safe even if it lags by at most one frame after a
      // debug-checkbox toggle.
      const showBlocks = this.debugFlags?.showBlocks || false;

      if (this.currentNetwork && (prevMouseX !== this.mouseX || prevMouseY !== this.mouseY)) {
        if (showBlocks || this.showSegmentColorsOnly) {
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

      if (this.currentNetwork && (prevMouseX !== -1 || prevMouseY !== -1)) {
        if (showBlocks || this.showSegmentColorsOnly) {
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
    // Refreshed once per flush from DOM debug checkboxes; hot paths
    // read this instead of querying the DOM per draw.
    this.debugFlags = { showBlocks: false, showTunnelLinks: false };
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
    // The per-frame sim snapshot (trains) is built by the sim overlay during the
    // live draw (see sim-overlay.ts) — the core renderer no longer reads the
    // stateManager singleton. With no overlay registered, no snapshot is built
    // and no sim state is drawn.
    // Refresh debug-checkbox state once per flush. Hot paths read
    // `this.debugFlags.X` instead of querying the DOM on every draw call.
    this._refreshDebugFlags();
    if (pending.world) this._drawWorld(this.currentNetwork);
    if (pending.live) this._drawLive(this.currentNetwork);
  }

  _refreshDebugFlags(): void {
    // The View menu's canvas layers (lib/canvas-layers.ts).
    const on = (id: LayerId) => canvasLayers.isOn(id);
    this.debugFlags = {
      showBlocks: on('blocks'),
      showTunnelLinks: on('tunnel-links'),
      showSwitches: on('switches'),
      showTileDirections: on('tile-directions'),
      showBlockDirections: on('block-dir'),
      showOneWayDirections: on('oneway-dir'),
      showBlockVoltage: on('block-voltage'),
      showBlockLength: on('block-length'),
      showBlockRadius: on('block-radius'),
      showBlockSpeedLimit: on('block-speed-limit'),
    };
  }

  /**
   * Set the derived per-network state (currentBlocks, currentSegments,
   * trackMap) so handlers in both world and live passes see consistent
   * data. Called once per flush by _flushRedraw before either layer
   * paints, and by the public drawNetwork(network) entry point.
   */
  _updateNetworkState(network: NetworkPayload | null): boolean {
    if (!network || !network.tracks) {
      this.currentNetwork = null;
      this.currentBlocks = {};
      this.currentSegments = {};
      this.trackMap = {};
      return false;
    }
    this.currentNetwork = network;
    this.currentBlocks = network.blocks || {};
    // Segment geometry rides nested inside each block's members, so the
    // geometry index is derived here rather than carried as a sibling map.
    this.currentSegments = segmentsFromBlocks(network.blocks);
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
    const p = getCanvasPalette();
    this.ctx.fillStyle = p.bg;
    this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
  }

  drawGrid(): void {
    // The whole grid layer hides together via the Display popover's "Show grid"
    // toggle: the repeating diamond lines, the origin axes, and the origin
    // marker (the coordinate reference). A no-op when hidden — the background
    // fill comes from clear(), not from here.
    if (!gridPreference.visible()) return;
    const p = getCanvasPalette();
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
    this.ctx.strokeStyle = p.grid;
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

    this.ctx.strokeStyle = p.axis;
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
    this.ctx.fillStyle = p.origin;
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
  fitToContent(
    tracks: ReadonlyArray<{ x: number; y: number }>,
    margin?: number,
    insets?: { left?: number; right?: number; top?: number; bottom?: number },
  ): void { this.camera.fitToContent(tracks, margin, insets); }

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

  /** Register a draw layer painted on top of the layout (e.g. the sim view). */
  registerOverlay(overlay: RendererOverlay): void {
    this._overlays.push(overlay);
  }

  /** Run every registered overlay for one z-order point of the live layer. */
  _emitOverlay(phase: OverlayPhase, network: NetworkPayload): void {
    for (const overlay of this._overlays) {
      overlay.draw(phase, network, this);
    }
  }

  /**
   * Paint the static world layer: background, grid, tracks, blocks, debug
   * overlays. Repainted only when the world flag is set (map shape / state
   * changes / viewport).
   */
  _drawWorld(network: NetworkPayload): void {
    this.ctx = this.worldCtx;
    this.clear();
    this.drawGrid();

    const tracks = network.tracks || [];

    // Draw segment ID colors BEFORE tracks (so they appear underneath). The
    // block-grouping overlay takes precedence: while it is on, tiles read by
    // owning-block color instead of per-segment geometry color.
    const showBlockDebug = this.debugFlags?.showBlocks || false;
    if (this.showBlockGroupingColors) {
      this.blockHandler.drawBlockGroupingColors(tracks, network.blocks);
    } else if (showBlockDebug || this.showSegmentColorsOnly) {
      this.segmentHandler.drawSegmentIdColors(tracks);
    }

    // Tracks (with state-driven coloring). Each tile draws its own grey
    // trapezoid bed then the block-status rectangle on top, both clipped to the
    // tile diamond (see trackHandler.drawTrack / drawTrackBed).
    tracks.forEach((track) => {
      this.trackHandler.drawTrack(track);
    });

    // Switch M/D indicators, gated behind the "Switch States" debug toggle
    // (default off). Drawn in a pass on TOP of every tile's rails so a
    // neighbouring tile's rail or bed can never paint over a badge.
    if (this.debugFlags?.showSwitches) {
      tracks.forEach((track) => {
        if (track.is_switch) this.trackHandler.drawSwitchBadgeAt(track);
      });
    }

    // Grey dots at block boundaries (gaps between different blocks).
    if (network.blocks) {
      this.segmentHandler.drawSegmentBoundaryMarkers(tracks);
    }

    // Tunnel mouths are NOT drawn here — they live on the live layer (see
    // _drawLive).

    // Other debug overlays.
    this.debugHandler.drawDebugOverlays(tracks);

    // Block info labels (full debug only).
    if (showBlockDebug && !this.showSegmentColorsOnly) {
      this.segmentHandler.drawSegmentDetailLabels(tracks);
    }

    // Per-block metric overlays (voltage, length, radius, speed limit). Each is
    // an independent opt-in toggle, but they render into ONE consolidated box
    // per block so enabling several stacks color-coded rows instead of
    // scattering a separate pill per metric.
    const mV = this.debugFlags?.showBlockVoltage;
    const mL = this.debugFlags?.showBlockLength;
    const mR = this.debugFlags?.showBlockRadius;
    const mS = this.debugFlags?.showBlockSpeedLimit;
    if (mV || mL || mR || mS) {
      this.segmentHandler.drawSegmentMetrics(tracks, { voltage: mV, length: mL, radius: mR, speed: mS });
    }
  }

  /**
   * Paint the dynamic live layer: tunnels, trains, box/lasso selection. Cleared with clearRect (transparent)
   * so the world canvas shows through. Repainted on every flush that has
   * the live flag set — i.e., hover, train movement, route changes.
   */
  _drawLive(network: NetworkPayload): void {
    this.ctx = this.liveCtx;
    this._clearLiveCanvas();

    // Rebuild the label-rect registry every live repaint.
    this.labelBounds = [];

    // Tunnel mouths (stud + label) and, for the selected pair, its connecting
    // line. The debug toggle shows every pairing line, otherwise only the selected
    // one (clicking a mouth is what reveals where it goes).
    this.tunnelHandler.drawTunnels(network.tunnels, this._pendingTunnelEndpoint || null, {
      showLinks: !!this.debugFlags?.showTunnelLinks,
      selected: this.selectedTunnel || null,
    });

    // The Editor's reachable-tile fill and locate flash.
    this.highlightHandler.draw();

    // Block direction arrows — only while the block editor is open. Drawn
    // below trains: it's editor context, not a live entity, so a moving train
    // paints over it.
    this.drawBlockDirectionOverlay();

    // Train overlay. Drawn above the block-direction overlay, below the
    // inspection debug arrows.
    this._emitOverlay('afterBlockDirection', network);

    // Per-tile absolute-direction debug arrows (operator-painted, inspection
    // only). Drawn above trains so a parked train can't hide a mark the
    // operator set on that tile.
    this.drawTileDirectionDebug();

    // One-way constraint arrows: amber per-tile arrows on blocks that allow only
    // one direction. Same inspection z-order as the forward-dir arrows above.
    this.drawOneWayDirectionDebug();

    // Box/lasso-select overlay (also paints the inspect "peek" highlight).
    this.drawBoxSelection();
    this.drawBoxSelectRect();
    this.drawLassoPath();

    // Editor build/hover previews, painted last so the ghost sits on top of
    // the live layer (as it did when the editor drew it after a redraw).
    this._drawBuildPreview();

    // Overlays that draw on top of everything.
    this._emitOverlay('top', network);
  }

  setBoxSelection(keysSet: Set<string> | null): void {
    this.boxSelection = keysSet || new Set();
  }

  setSelectionGroups(groups: SelectionOutlineGroup[] | null): void {
    this.selectionGroups = groups || [];
  }

  // Set (or clear, with []) the inspect peek highlight. Only the live layer
  // needs repainting — the outline draws on top of the world.
  setInspectHighlight(groups: SelectionOutlineGroup[] | null): void {
    this.inspectHighlightGroups = groups || [];
    this.requestRedraw({ world: false, live: true, reason: 'inspect-highlight' });
  }

  setBoxSelectRect(rect: { startX: number; startY: number; endX: number; endY: number } | null): void {
    this.boxSelectRect = rect;
  }

  setLassoPath(points: Array<{ x: number; y: number }> | null): void {
    this.lassoPath = points;
  }

  setBuildPreview(preview: BuildPreview | null): void {
    this.buildPreview = preview;
  }

  _drawBuildPreview(): void {
    const p = this.buildPreview;
    if (!p) return;
    switch (p.kind) {
      case 'tracks':
        this.trackHandler.drawPreviewTracks(p.tracks);
        break;
      case 'eraser':
        this.trackHandler.drawEraserPreviewPath(p.path);
        break;
      case 'segment':
        this.segmentHandler.drawSegmentPreviewPath(p.path);
        break;
      case 'path':
        this.trackHandler.drawPreviewPath(p.path);
        break;
      case 'invalid':
        this.trackHandler.drawInvalidPreviewPath(p.path);
        break;
    }
  }

  setPendingTunnelEndpoint(endpoint: { x: number; y: number; edge: string } | null): void {
    this._pendingTunnelEndpoint = endpoint;
    this.requestRedraw({ world: true, live: true, reason: 'tunnel-pending' });
  }

  setSelectedTunnel(pair: TunnelPair | null): void {
    this.selectedTunnel = pair || null;
    // Tunnels draw on the live layer, so only that needs repainting.
    this.requestRedraw({ world: false, live: true, reason: 'tunnel-select' });
  }

  /** track_bits of the tile at (x,y), or 0 — used to orient the tunnel stud. */
  trackBitsAt(x: number, y: number): number {
    return this.trackMap[`${x},${y}`]?.track_bits ?? 0;
  }

  // Committed box/lasso/paint selection. Dims each tile and lights the real
  // track stroke + run ends through the shared overlay, so a chain of diagonal
  // tiles reads as the line of track it is, not a zigzag of cells. Amber keeps
  // the selection identity distinct from the purple drag-build preview.
  drawBoxSelection(): void {
    const p = getCanvasPalette();
    this.trackHandler.drawTileExtentHighlight(this.boxSelection, {
      tileFill: withAlpha(p.caution, 0.14),
      glow: withAlpha(p.warning, 0.26),
      core: withAlpha(p.caution, 0.95),
      cap: withAlpha(p.text, 0.95),
    });
    this.drawSelectionEntityOutlines();
    // Inspect "peek" highlight — a single entity outlined from a panel
    // cross-link (switch editor flank / train-protection links). Same dashed
    // rail as the committed selection above, but no "selected tile" amber fill,
    // and driven by its own group set so it shows even with nothing selected.
    this.drawSelectionEntityOutlines(this.inspectHighlightGroups);
  }

  // Per-entity rail tint for the committed selection, painted over the amber
  // track overlay above. Each block / switch the selection
  // touches re-strokes its own tiles' rail in its panel-chip hue + dash, so
  // membership reads as a colored line of track rather than a tile perimeter.
  // The amber rail underneath still says "selected" and its end-caps still mark
  // where each run stops. Group color / width / dash / order come straight from
  // _buildSelectionOutlineGroups: later groups paint on top (the block is drawn
  // last as a thin, light line so it stays a delicate marker over the wider
  // dashed switch beneath it).
  drawSelectionEntityOutlines(groups: SelectionOutlineGroup[] = this.selectionGroups): void {
    if (!groups || groups.length === 0) return;
    const half = (this.gridSize * this.scale) / 2;
    const trackMap = this.trackMap || {};
    const edgePoints = this.trackHandler.getEdgePoints(half);
    // A fixed-width translucent aura under each group's core, so even a thin
    // line (the block, width 2) tints the whole rail its color and reads at a
    // glance — the precise solid/dashed core then sits on top.
    const auraWidth = TRACK_WIDTH * 2.6 * this.scale;
    this.ctx.save();
    this.ctx.lineJoin = 'round';
    this.ctx.lineCap = 'round';
    const strokeGroupRail = (g: SelectionOutlineGroup): void => {
      for (const key of g.keys) {
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
    };
    for (const g of groups) {
      if (g.keys.size === 0) continue;
      this.ctx.strokeStyle = g.color;
      // Aura pass (solid, translucent, fixed wide), then the core pass (full
      // strength, builder width + dash).
      this.ctx.globalAlpha = 0.3;
      this.ctx.lineWidth = auraWidth;
      this.ctx.setLineDash([]);
      strokeGroupRail(g);
      this.ctx.globalAlpha = 1;
      this.ctx.lineWidth = g.width * this.scale;
      this.ctx.setLineDash(g.dash.map((d) => d * this.scale));
      strokeGroupRail(g);
    }
    this.ctx.globalAlpha = 1;
    this.ctx.setLineDash([]);
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
    const p = getCanvasPalette();
    this.ctx.save();
    this.ctx.fillStyle = withAlpha(p.caution, 0.10);
    this.ctx.strokeStyle = withAlpha(p.warning, 0.90);
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
    const p = getCanvasPalette();
    this.ctx.save();
    // Filled interior of the in-progress polygon, lightly shaded.
    this.ctx.fillStyle = withAlpha(p.caution, 0.10);
    this.ctx.beginPath();
    this.ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) this.ctx.lineTo(pts[i].x, pts[i].y);
    this.ctx.closePath();
    this.ctx.fill();
    // Outline of the drawn path.
    this.ctx.strokeStyle = withAlpha(p.warning, 0.90);
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
    this.highlightHandler.setReachableHighlights(positionsSet);
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
    this.highlightHandler.flashTiles(tiles, opts);
  }

  /**
   * Focus the block direction arrows on one block (the one the block editor is
   * editing) so its forward / reverse arrows show on the map even when the
   * Debug panel's "Block dir" toggle is off. Pass null to clear.
   */
  setBlockDirectionFocus(blockId: string | null): void {
    this._blockDirectionFocusId = blockId || null;
    this.requestRedraw({ world: false, live: true, reason: 'block-direction-focus' });
  }

  clearBlockDirectionFocus(): void {
    if (!this._blockDirectionFocusId) return;
    this._blockDirectionFocusId = null;
    this.requestRedraw({ world: false, live: true, reason: 'block-direction-focus-clear' });
  }

  /**
   * Block forward / reverse direction arrows. For each block in scope, draw a
   * green F arrow at the forward terminal and a cyan R arrow at the reverse
   * terminal — each tracing that terminal tile's track segment and pointing out
   * of the block, the same edge-to-edge style as the per-tile arrows. A
   * direction not in the block's `allowed_travel_directions` is drawn dimmed.
   * Scope is every block when the Debug panel's "Block dir" toggle is on,
   * otherwise just the block the editor is focused on (if any).
   */
  drawBlockDirectionOverlay(): void {
    const blocks = this.currentBlocks;
    if (!blocks || !this.trackMap) return;
    const p = getCanvasPalette();
    let ids: string[];
    if (this.debugFlags?.showBlockDirections) {
      ids = Object.keys(blocks);
    } else if (this._blockDirectionFocusId && blocks[this._blockDirectionFocusId]) {
      ids = [this._blockDirectionFocusId];
    } else {
      return;
    }
    for (const id of ids) {
      const block: any = blocks[id];
      const ordered = block && block.ordered_tiles;
      if (!Array.isArray(ordered) || ordered.length === 0) continue;
      const allowed: string[] = Array.isArray(block.allowed_travel_directions)
        ? block.allowed_travel_directions
        : ['forward', 'reverse'];
      // forward = success-green, reverse = info-cyan — two distinct accents.
      const fwd = blockTerminalArrowSpec(ordered, this.trackMap, 'forward');
      const rev = blockTerminalArrowSpec(ordered, this.trackMap, 'reverse');
      // A single-tile block's forward and reverse arrows trace the same segment
      // in opposite directions, so they would land on top of each other. Shift
      // each into its own lane to pull them apart. Multi-tile blocks put F and R
      // on different tiles already, so they need no shift.
      const lane = ordered.length === 1 ? ((this.gridSize * this.scale) / 2) * 0.4 : 0;
      if (fwd) {
        this._drawTileForwardArrow(fwd.x, fwd.y, fwd.entryEdge, fwd.exitEdge, p.success, {
          letter: 'F',
          dimmed: !allowed.includes('forward'),
          laneOffset: lane,
        });
      }
      if (rev) {
        this._drawTileForwardArrow(rev.x, rev.y, rev.entryEdge, rev.exitEdge, p.info, {
          letter: 'R',
          dimmed: !allowed.includes('reverse'),
          laneOffset: lane,
        });
      }
    }
  }

  /**
   * Per-tile forward-direction debug arrows. For every block, draw a green
   * arrow along each of its tiles, running from the edge forward travel enters
   * to the edge it exits — the per-tile realization of the block's forward /
   * reverse. Direction comes from the tile's place in `ordered_tiles`, and the
   * arrow traces the tile's actual track segment, so it sits on the track,
   * follows its direction, and is inset so it never crosses the tile boundary.
   * Backward is simply the opposite. Inspection only, gated by the Debug
   * panel's "Forward dir" toggle, and only drawn for tiles in the network.
   */
  drawTileDirectionDebug(): void {
    if (!this.debugFlags?.showTileDirections) return;
    this._drawBlockDirectionArrows('forward');
  }

  /**
   * One-way block direction arrows. For every block whose travel is restricted
   * to a single direction (not "both"), draw an amber arrow along each of its
   * tiles pointing the way travel is actually allowed: forward-only points the
   * same way as the "Forward dir" arrows, reverse-only points the opposite way
   * (its arrowheads read as "<"). Blocks that allow both directions are skipped,
   * so the only marks on screen are the one-way constraints. Amber sets these
   * apart from the green forward / cyan reverse arrows. Same per-tile,
   * on-the-track geometry as drawTileDirectionDebug. Inspection only, gated by
   * the Debug panel's "One-way dir" toggle.
   */
  drawOneWayDirectionDebug(): void {
    if (!this.debugFlags?.showOneWayDirections) return;
    this._drawBlockDirectionArrows('oneway');
  }

  /**
   * Shared per-tile direction-arrow overlay behind the Forward-dir and
   * One-way-dir debug toggles. For each block (every block in `'forward'` mode,
   * only one-way blocks in `'oneway'` mode) draws an arrow along each tile from
   * the edge forward travel enters to the edge it exits, using forwardArrowEdges
   * for the on-track geometry. `'forward'` is green and dims blocks that forbid
   * forward travel; `'oneway'` is amber and swaps the arrow for reverse-only
   * blocks so the head points the way travel is allowed.
   */
  private _drawBlockDirectionArrows(mode: 'forward' | 'oneway'): void {
    const blocks = this.currentBlocks;
    if (!blocks || !this.trackMap) return;
    const p = getCanvasPalette();
    for (const block of Object.values(blocks) as any[]) {
      const ordered = block && block.ordered_tiles;
      if (!Array.isArray(ordered) || ordered.length === 0) continue;
      const allowed: string[] = Array.isArray(block.allowed_travel_directions)
        ? block.allowed_travel_directions
        : ['forward', 'reverse'];
      const hasForward = allowed.includes('forward');
      const hasReverse = allowed.includes('reverse');
      // 'oneway' mode draws only one-way blocks: skip both-allowed (the common
      // case) and, defensively, neither-allowed — no single direction to point.
      if (mode === 'oneway' && hasForward === hasReverse) continue;
      const dimmed = mode === 'forward' && !hasForward;
      const color = mode === 'forward' ? p.success : p.warning;
      const n = ordered.length;
      for (let i = 0; i < n; i++) {
        const x = ordered[i][0];
        const y = ordered[i][1];
        const track = this.trackMap[`${x},${y}`];
        if (!track) continue;
        const storedEntry = String(ordered[i][2] || '').toUpperCase();
        const arrivalDir =
          i > 0 ? compassFromDelta(x - ordered[i - 1][0], y - ordered[i - 1][1]) : null;
        const departDir =
          i < n - 1 ? compassFromDelta(ordered[i + 1][0] - x, ordered[i + 1][1] - y) : null;
        const edges = forwardArrowEdges(track.track_bits || 0, storedEntry, arrivalDir, departDir);
        if (!edges) continue;
        // forwardArrowEdges gives [entry, exit] for the forward direction, with
        // the arrowhead at exit. A reverse-only one-way block traces the same
        // segment but swaps the two so the head points backward along the tile
        // order.
        const [a, b] = mode === 'oneway' && !hasForward ? [edges[1], edges[0]] : edges;
        this._drawTileForwardArrow(x, y, a, b, color, mode === 'forward' ? { dimmed } : {});
      }
    }
  }

  /**
   * Draw one direction arrow along a tile's track segment: a shaft from the
   * entry-edge midpoint to the exit-edge midpoint with the arrowhead at the
   * exit end. Endpoints are the exact edge midpoints — the same geometry the
   * track renderer uses — so the arrow lies on the track centerline and meets
   * the neighbouring tiles' arrows at the shared edge, keeping a straight run's
   * arrows collinear instead of bobbing tile to tile. `opts.letter` adds a
   * centered F / R pill (for the block arrows); `opts.dimmed` fades the whole
   * arrow (a direction the block does not allow).
   */
  _drawTileForwardArrow(
    x: number,
    y: number,
    entryEdge: string,
    exitEdge: string,
    color: string,
    opts: { letter?: string; dimmed?: boolean; laneOffset?: number } = {}
  ): void {
    const cx = this.gridToScreenX(x, y);
    const cy = this.gridToScreenY(x, y);
    const half = (this.gridSize * this.scale) / 2;
    const edges = this.trackHandler.getEdgePoints(half);
    const e1 = edges[String(entryEdge).toUpperCase()];
    const e2 = edges[String(exitEdge).toUpperCase()];
    if (!e1 || !e2) return;

    // Direction + perpendicular from the edge offsets alone. A lane shift below
    // slides the arrow sideways without rotating it, so these stay valid.
    const dx = e2.x - e1.x;
    const dy = e2.y - e1.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len;
    const uy = dy / len;
    const px = -uy;
    const py = ux;

    const p = getCanvasPalette();
    this.ctx.save();

    // F / R pill box, measured up front because its size also sets the minimum
    // lane shift below. Stays zero when there is no letter (the plain per-tile
    // arrows). Measuring needs the font, so set it here, inside the save().
    const fontPx = Math.max(8, 9 * this.scale);
    const padX = 3 * this.scale;
    const padY = 2 * this.scale;
    let boxW = 0;
    let boxH = 0;
    if (opts.letter) {
      this.ctx.font = `bold ${fontPx}px sans-serif`;
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'middle';
      boxW = this.ctx.measureText(opts.letter).width + padX * 2;
      boxH = fontPx + padY * 2;
    }

    // Optional sideways shift, perpendicular to the arrow's own heading. Callers
    // pass the same positive offset to a single-tile block's forward and reverse
    // arrows — which trace one segment in opposite directions — so they slide to
    // opposite sides instead of landing on top of each other. When a pill rides
    // along, widen the shift so the two pills can't overlap: they sit symmetric
    // about the tile center (so 2*lane apart), and a pill-diagonal of separation
    // clears them at any track angle. The pill is a fixed pixel size, so this
    // floor — not a fraction of the tile — is what keeps them apart when zoomed
    // out. Zero offset keeps the arrow on the track centerline, so a straight
    // run's per-tile arrows stay collinear.
    let lane = opts.laneOffset || 0;
    if (lane > 0 && opts.letter) lane = Math.max(lane, Math.hypot(boxW, boxH) / 2);
    const ox = px * lane;
    const oy = py * lane;
    const tailX = cx + e1.x + ox;
    const tailY = cy + e1.y + oy;
    const tipX = cx + e2.x + ox;
    const tipY = cy + e2.y + oy;
    const headLen = Math.min(Math.max(5, half * 0.3), len * 0.6);
    const headW = headLen * 0.52;
    const lineW = Math.min(Math.max(2, this.scale * 0.9), 4);

    if (opts.dimmed) this.ctx.globalAlpha = 0.25;
    this.ctx.strokeStyle = color;
    this.ctx.fillStyle = color;
    this.ctx.lineWidth = lineW;
    this.ctx.lineCap = 'round';
    this.ctx.lineJoin = 'round';

    // Shaft (stops just short of the tip so the arrowhead caps it cleanly).
    this.ctx.beginPath();
    this.ctx.moveTo(tailX, tailY);
    this.ctx.lineTo(tipX - ux * headLen * 0.9, tipY - uy * headLen * 0.9);
    this.ctx.stroke();

    // Arrowhead at the forward (exit) end.
    this.ctx.beginPath();
    this.ctx.moveTo(tipX, tipY);
    this.ctx.lineTo(tipX - ux * headLen + px * headW, tipY - uy * headLen + py * headW);
    this.ctx.lineTo(tipX - ux * headLen - px * headW, tipY - uy * headLen - py * headW);
    this.ctx.closePath();
    this.ctx.fill();

    // Optional F / R glyph in a dark pill at the tile center, so the block's
    // forward / reverse arrows read distinctly from the plain per-tile ones.
    if (opts.letter) {
      // Pill rides in the same lane as its arrow, so a single-tile block's F and
      // R glyphs separate instead of overlapping at the tile center. Font and box
      // metrics were set above (they also sized the lane shift).
      const lx = cx + ox;
      const ly = cy + oy;
      this.ctx.fillStyle = withAlpha(p.overlayBg, 0.9);
      if (this.ctx.roundRect) {
        this.ctx.beginPath();
        this.ctx.roundRect(lx - boxW / 2, ly - boxH / 2, boxW, boxH, 3);
        this.ctx.fill();
      } else {
        this.ctx.fillRect(lx - boxW / 2, ly - boxH / 2, boxW, boxH);
      }
      this.ctx.strokeStyle = color;
      this.ctx.lineWidth = Math.min(Math.max(1, this.scale * 0.4), 1.5);
      if (this.ctx.roundRect) {
        this.ctx.beginPath();
        this.ctx.roundRect(lx - boxW / 2, ly - boxH / 2, boxW, boxH, 3);
        this.ctx.stroke();
      }
      this.ctx.fillStyle = color;
      this.ctx.fillText(opts.letter, lx, ly);
    }
    this.ctx.restore();
  }
}

export { Renderer };
