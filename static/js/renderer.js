// renderer.js - Isometric Railway Renderer
// ============================================================================
// Requires: constants.js (must be loaded first)
// ============================================================================

// ============================================================================
// Isometric Geometry Utility
// Centralized handling of all track segment and edge rotation math
// ============================================================================

const COLORS = {
    BACKGROUND: '#0a0a0c',
    GRID_LINES: '#2a2a34',
    AXIS_LINES: '#4a4a5a',
    ORIGIN_MARKER: '#8257e5'
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
     * @param {string} e1 - First edge ('N', 'E', 'S', 'W')
     * @param {string} e2 - Second edge ('N', 'E', 'S', 'W')
     * @returns {number} Rotation angle in radians
     */
    getSegmentAngle(e1, e2) {
        const segment = [e1, e2].sort().join('');
        const angles = {
            'NS': -Math.PI / 4,   // -45° (135°)
            'EW': Math.PI / 4,    // +45°
            'EN': Math.PI / 2,    // 90° (vertical) - NE
            'NW': 0,              // 0° (horizontal)
            'ES': 0,              // 0° (horizontal) - SE
            'SW': Math.PI / 2     // 90° (vertical)
        };
        return angles[segment] || 0;
    },
};

class Renderer {
    constructor(canvasId) {
        // Phase 5: two stacked canvases — world (static map shape) below,
        // live (trains, routes, hover, selection) above. The legacy
        // canvasId (#rail-canvas) is the live canvas because mouse events
        // fire on the topmost element. The world canvas is paired by id.
        this.liveCanvas = document.getElementById(canvasId);
        this.worldCanvas = document.getElementById('rail-world-canvas');
        if (!this.liveCanvas || !this.worldCanvas) {
            console.error('[Renderer] Required canvas elements not found!');
            return;
        }
        // `this.canvas` historically points to the single canvas. Keep it
        // pointing at the live canvas so existing code (mouse handlers,
        // getBoundingClientRect, dimensions) keeps working without changes.
        this.canvas = this.liveCanvas;
        this.liveCtx = this.liveCanvas.getContext('2d');
        this.worldCtx = this.worldCanvas.getContext('2d');
        // `this.ctx` is the *active* context — handler files draw to it.
        // _drawWorld and _drawLive switch this before calling handlers so
        // handler code (renderer-tracks.js, renderer-trains.js, …) stays
        // ignorant of which layer it's painting to.
        this.ctx = this.worldCtx;

        // Viewport (pan, zoom, projection) lives on Camera so editor / app /
        // handler files all read & write through one canonical object. See
        // static/js/camera.js for the math.
        this.camera = new Camera(this.canvas);

        // Mouse position for hover detection
        this.mouseX = -1;
        this.mouseY = -1;
        this.platformBounds = []; // Array of {bounds, name, abbrev} for hover detection

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
            const hasTrains = this.currentNetwork?.trains?.length > 0;

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
            const hasTrains = this.currentNetwork?.trains?.length > 0;

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
        this.debugFlags = { showBlocks: false, showSignals: false };
        this.resize();
        window.addEventListener('resize', () => this._scheduleResize());

        // Watch the canvas's parent for size changes that don't fire a window
        // resize — e.g., the secondary sidebar opening/closing changes
        // .canvas-container's clientWidth via flex reflow without dispatching
        // a window resize event.
        if (typeof ResizeObserver !== 'undefined' && this.canvas.parentElement) {
            this._resizeObserver = new ResizeObserver(() => this._scheduleResize());
            this._resizeObserver.observe(this.canvas.parentElement);
        }
    }

    _scheduleResize() {
        if (this._pendingResizeFrame !== null) return;
        this._pendingResizeFrame = requestAnimationFrame(() => {
            this._pendingResizeFrame = null;
            this.resize();
        });
    }

    resize() {
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
        this.liveCanvas.width = width;
        this.liveCanvas.height = height;
        this.worldCanvas.width = width;
        this.worldCanvas.height = height;

        // Camera updates centerX / centerY from the new canvas dimensions.
        // It does NOT touch panX / panY — see camera.js resizeToCanvas for
        // the rationale (preserves the world coord at the visual center).
        this.camera.resizeToCanvas();

        // Resize is treated as a world+live invalidation; route through the
        // rAF coalescer so other state changes in the same frame share the
        // paint pass.
        this.requestRedraw({ world: true, live: true, reason: 'resize' });
    }

    /**
     * Schedule a redraw on the next animation frame. Synchronous callers
     * in the same frame OR their layer flags together; the actual paint
     * happens once per rAF tick.
     *
     * Options:
     *   world   — repaint the static world layer (tracks, blocks, stations,
     *             platforms, signal positions). Phase 5 will make this
     *             skip-able; in Phase 3 it collapses with `live`.
     *   live    — repaint the dynamic live layer (trains, signal aspects,
     *             route overlays, hover). Same Phase-5 caveat.
     *   reason  — optional dev label for _lastFlushReason; aids profiling.
     *   network — if provided, update this.currentNetwork before flush.
     *             Used by App.refreshNetwork for the fresh-network case.
     */
    requestRedraw(opts = {}) {
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

    _flushRedraw() {
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
        // window.stateManager directly (Phase 6).
        this.currentSnapshot = (typeof buildRenderSnapshot === 'function')
            ? buildRenderSnapshot(this.currentNetwork, window.stateManager)
            : { trains: {} };
        // Refresh debug-checkbox state once per flush. Hot paths read
        // `this.debugFlags.X` instead of querying the DOM on every draw call.
        this._refreshDebugFlags();
        if (pending.world) this._drawWorld(this.currentNetwork);
        if (pending.live) this._drawLive(this.currentNetwork);
    }

    _refreshDebugFlags() {
        const showBlocks = document.getElementById('debug-show-blocks');
        const showSignals = document.getElementById('debug-show-signals');
        this.debugFlags = {
            showBlocks: showBlocks ? !!showBlocks.checked : false,
            showSignals: showSignals ? !!showSignals.checked : false,
        };
    }

    /**
     * Set the derived per-network state (currentBlocks, currentPlatforms,
     * trackMap) so handlers in both world and live passes see consistent
     * data. Called once per flush by _flushRedraw before either layer
     * paints, and by the public drawNetwork(network) entry point.
     */
    _updateNetworkState(network) {
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
        network.tracks.forEach(t => {
            this.trackMap[`${t.x},${t.y}`] = t;
        });
        return true;
    }

    _clearLiveCanvas() {
        // Live canvas uses clearRect (transparent) so the world canvas
        // shows through. Background fill belongs only on the world canvas.
        this.liveCtx.clearRect(0, 0, this.liveCanvas.width, this.liveCanvas.height);
    }

    draw() {
        // Basic draw - just clear and draw grid if no network loaded
        this.clear();
        this.drawGrid();
    }

    clear() {
        this.ctx.fillStyle = COLORS.BACKGROUND;
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    }

    drawGrid() {
        this.ctx.save();

        // 1:1 square diamond tile (rotated 45 degrees)
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;
        const quarter = tileSize / 4;

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
        const gridPointX = (gx, gy) => this.centerX + this.panX + (gx + gy) * tileSize / 2;
        const gridPointY = (gx, gy) => this.centerY + this.panY + (gx - gy) * tileSize / 2;

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

    // Projection and viewport math live on Camera (static/js/camera.js).
    // The thin delegators below let internal Renderer methods and handler
    // getters (e.g. `renderer-trains.js` get scale, gridToScreenX) keep
    // their existing call sites while the canonical state moves to Camera.
    // Any new code should call `this.camera.<method>` directly.

    get panX() { return this.camera.panX; }
    set panX(v) { this.camera.panX = v; }
    get panY() { return this.camera.panY; }
    set panY(v) { this.camera.panY = v; }
    get scale() { return this.camera.scale; }
    set scale(v) { this.camera.scale = v; }
    get centerX() { return this.camera.centerX; }
    get centerY() { return this.camera.centerY; }
    get gridSize() { return this.camera.gridSize; }

    gridToScreenX(gridX, gridY = 0) { return this.camera.gridToScreenX(gridX, gridY); }
    gridToScreenY(gridX, gridY = 0) { return this.camera.gridToScreenY(gridX, gridY); }
    screenToGridX(screenX, screenY) { return this.camera.screenToGridX(screenX, screenY); }
    screenToGridY(screenX, screenY) { return this.camera.screenToGridY(screenX, screenY); }
    screenToGrid(screenX, screenY) { return this.camera.screenToGrid(screenX, screenY); }
    centerOnContent(tracks) { this.camera.centerOnContent(tracks); }

    /**
     * Public entry point for a full repaint (both layers). Used by callers
     * that need synchronous immediate paint — most code should go through
     * requestRedraw() so paints coalesce on rAF.
     */
    drawNetwork(network) {
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
    _drawWorld(network) {
        this.ctx = this.worldCtx;
        this.clear();
        this.drawGrid();

        // Draw block ID colors BEFORE tracks (so they appear underneath).
        // Show if: debug checkbox is checked OR showBlockColorsOnly is set.
        const showBlockDebug = this.debugFlags?.showBlocks || false;
        if (showBlockDebug || this.showBlockColorsOnly) {
            this.blockHandler.drawBlockIdColors(network.tracks);
        }

        // Tracks (with state-driven coloring).
        network.tracks.forEach(track => {
            this.trackHandler.drawTrack(track, false);
        });

        // Grey dots at block boundaries (gaps between different blocks).
        if (network.blocks) {
            this.blockHandler.drawBlockBoundaryMarkers(network.tracks);
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
            this.signalHandler.drawSignalBlockConnections(network.tracks);
        }

        // Signals on top of connection lines.
        network.tracks.forEach(track => {
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

        // Other debug overlays.
        this.debugHandler.drawDebugOverlays(network.tracks);

        // Block info labels (full debug only).
        if (showBlockDebug && !this.showBlockColorsOnly) {
            this.blockHandler.drawBlockDetailLabels(network.tracks);
        }
    }

    /**
     * Paint the dynamic live layer: route overlays, trains, station hover
     * highlight, box/lasso selection. Cleared with clearRect (transparent)
     * so the world canvas shows through. Repainted on every flush that has
     * the live flag set — i.e., hover, train movement, route changes.
     */
    _drawLive(network) {
        this.ctx = this.liveCtx;
        this._clearLiveCanvas();

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

        // Trains.
        if (network.trains) {
            network.trains.forEach(train => {
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

    setBoxSelection(keysSet) {
        this.boxSelection = keysSet || new Set();
    }

    setBoxSelectRect(rect) {
        this.boxSelectRect = rect;
    }

    setLassoPath(points) {
        this.lassoPath = points;
    }

    drawBoxSelection() {
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

    drawBoxSelectRect() {
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

    drawLassoPath() {
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

    setReachableHighlights(positionsSet) {
        this.routeHandler.setReachableHighlights(positionsSet);
    }

    clearRoutePreview() {
        this.routeHandler.clearRoutePreview();
    }

    flashConflictTiles(tiles, durationMs = 2000) {
        this.routeHandler.flashConflictTiles(tiles, durationMs);
    }

    highlightPath(path) {
        this.routeHandler.highlightRoute(path);
    }

    clearPathHighlight() {
        this.routeHandler.clearRouteHighlight();
    }

    // ========================================================================
    // Label placement helpers (shared by train + platform labels)
    // ========================================================================

    getNearbyTrackSegments(cx, cy, radius) {
        const tracks = this.currentNetwork?.tracks;
        if (!tracks || !tracks.length) return [];
        const half = this.gridSize * this.scale / 2;
        const edges = {
            'N': { x:  half / 2, y: -half / 2 },
            'E': { x:  half / 2, y:  half / 2 },
            'S': { x: -half / 2, y:  half / 2 },
            'W': { x: -half / 2, y: -half / 2 },
        };
        const cutoff = (radius + half) * (radius + half);
        const segments = [];
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

    distPointToSegment(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1, dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        if (lenSq < 1e-6) return Math.hypot(px - x1, py - y1);
        let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
        t = Math.max(0, Math.min(1, t));
        return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
    }

    distRectToSegments(bx, by, w, h, segments) {
        if (!segments || !segments.length) return Infinity;
        const cx = bx + w / 2, cy = by + h / 2;
        const samples = [
            [cx, cy],
            [bx, by], [bx + w, by], [bx, by + h], [bx + w, by + h],
            [cx, by], [cx, by + h], [bx, cy], [bx + w, cy],
        ];
        let minDist = Infinity;
        for (const seg of segments) {
            for (const [px, py] of samples) {
                const d = this.distPointToSegment(px, py, seg.x1, seg.y1, seg.x2, seg.y2);
                if (d < minDist) minDist = d;
            }
        }
        return minDist;
    }
}
