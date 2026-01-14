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

    /**
     * Get the angle perpendicular to an edge grid line (inward toward tile).
     * For short paths, half-circles should align perpendicular to the edge.
     * @param {string} edge - Edge name ('N', 'E', 'S', 'W')
     * @returns {number} Angle in radians pointing inward from edge
     */
    getEdgePerpendicularAngle(edge) {
        // Edge orientations in isometric grid (45° rotated):
        // N and S edges run diagonally (one orientation)
        // E and W edges run diagonally (perpendicular orientation)
        const angles = {
            'N': -Math.PI / 4,    // N edge: perpendicular points down-left (-45°)
            'E': Math.PI / 4,     // E edge: perpendicular points up-left (45°)
            'S': Math.PI * 3 / 4, // S edge: perpendicular points up-right (135°)
            'W': -Math.PI * 3 / 4 // W edge: perpendicular points down-right (-135° or 225°)
        };
        return angles[edge] || 0;
    },

    /**
     * Draw a rectangle aligned with a track segment or edge.
     * The rectangle is rotated to match the segment's orientation in screen space.
     * 
     * @param {CanvasRenderingContext2D} ctx - Canvas context
     * @param {number} cx - Center X in screen coordinates
     * @param {number} cy - Center Y in screen coordinates
     * @param {number} width - Width (perpendicular to segment)
     * @param {number} height - Height (parallel to segment)
     * @param {string} e1 - First edge of segment ('N', 'E', 'S', or 'W')
     * @param {string} e2 - Second edge of segment (optional, defaults to opposite of e1)
     */
    drawAlignedRect(ctx, cx, cy, width, height, e1, e2 = null) {
        ctx.save();
        ctx.translate(cx, cy);

        // If e2 not provided, assume we want alignment perpendicular to the edge
        // For boundary markers, we need to know the actual segment direction
        // Use the segment angle to rotate the rectangle
        const angle = this.getSegmentAngle(e1, e2 || e1);
        ctx.rotate(angle);

        // Draw rectangle centered at origin (after rotation)
        ctx.fillRect(-width / 2, -height / 2, width, height);

        ctx.restore();
    }
};

class Renderer {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) {
            console.error('[Renderer] Canvas element not found!');
            return;
        }
        this.ctx = this.canvas.getContext('2d');
        this.gridSize = 40;

        // Pan offset (user dragging)
        this.panX = 0;
        this.panY = 0;
        this.scale = 1.0;

        // Center of canvas (where grid 0,0 is displayed)
        this.centerX = 0;
        this.centerY = 0;

        // Mouse position for hover detection
        this.mouseX = -1;
        this.mouseY = -1;
        this.stationBounds = []; // Array of {bounds, name, abbrev} for hover detection

        // Block display mode:
        // - false: only show when debug checkbox is checked (full debug info)
        // - true: show block colors only (for block tools)
        this.showBlockColorsOnly = false;

        // Block section colors (semi-transparent versions of track colors)
        // FREE=Blue, RESERVED=Green, OCCUPIED=Red, CAUTION=Yellow
        this.blockColors = {
            'FREE': 'rgba(66, 133, 244, 0.3)',      // Blue
            'RESERVED': 'rgba(4, 211, 97, 0.3)',    // Green
            'OCCUPIED': 'rgba(232, 63, 91, 0.3)',   // Red
            'CAUTION': 'rgba(255, 205, 30, 0.3)'    // Yellow
        };

        // Current blocks data for track coloring
        this.currentBlocks = {};

        // Block color cache for golden angle distribution
        // Maps block_id -> assigned hue (ensures consistent colors across renders)
        this.blockColorCache = {};
        this.blockColorCounter = 0;  // Increments for each new block

        // Store current network for redraws
        this.currentNetwork = null;

        this.trackHandler = new RendererTrackHandler(this);
        this.signalHandler = new RendererSignalHandler(this);
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
            // Need redraw for: block hover, station name hover, etc.
            const showBlocksElement = document.getElementById('debug-show-blocks');
            const showBlocks = showBlocksElement ? showBlocksElement.checked : false;
            const hasStations = this.currentStations && this.currentStations.length > 0;

            if (this.currentNetwork && (prevMouseX !== this.mouseX || prevMouseY !== this.mouseY)) {
                // Redraw if blocks debug is enabled OR if there are stations (for hover-to-expand)
                if (showBlocks || this.showBlockColorsOnly || hasStations) {
                    this.drawNetwork(this.currentNetwork);
                }
            }
        });
        this.canvas.addEventListener('mouseleave', () => {
            const prevMouseX = this.mouseX;
            const prevMouseY = this.mouseY;
            this.mouseX = -1;
            this.mouseY = -1;

            // Redraw when mouse leaves to clear hover state
            const showBlocksElement = document.getElementById('debug-show-blocks');
            const showBlocks = showBlocksElement ? showBlocksElement.checked : false;
            const hasStations = this.currentStations && this.currentStations.length > 0;

            if (this.currentNetwork && (prevMouseX !== -1 || prevMouseY !== -1)) {
                if (showBlocks || this.showBlockColorsOnly || hasStations) {
                    this.drawNetwork(this.currentNetwork);
                }
            }
        });

        this.resize();
        window.addEventListener('resize', () => this.resize());
    }

    resize() {
        const parent = this.canvas.parentElement;

        // Get dimensions, with fallbacks
        let width = parent ? parent.clientWidth : window.innerWidth;
        let height = parent ? parent.clientHeight : window.innerHeight;

        // Ensure minimum size
        width = Math.max(width, 100);
        height = Math.max(height, 100);

        this.canvas.width = width;
        this.canvas.height = height;

        // Center point of the canvas (where grid 0,0 appears)
        this.centerX = Math.floor(width / 2);
        this.centerY = Math.floor(height / 2);

        this.draw();
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

    // Convert grid coordinates to screen X/Y (isometric/diamond projection)
    // Updated convention: X+ goes down-right, Y+ goes up-right.
    // Corner-based: tile (x,y) has corners at (x,y), (x+1,y), (x+1,y+1), (x,y+1)
    // Tile center is at (x+0.5, y+0.5), so we add 1 to (gridX + gridY)
    gridToScreenX(gridX, gridY = 0) {
        const tileSize = this.gridSize * this.scale;
        return this.centerX + this.panX + ((gridX + gridY + 1) * tileSize / 2);
    }

    gridToScreenY(gridX, gridY = 0) {
        const tileSize = this.gridSize * this.scale;
        // screenY = (gridX - gridY) * halfTile
        return this.centerY + this.panY + ((gridX - gridY) * tileSize / 2);
    }

    // Convert screen coordinates to grid X (inverse isometric projection)
    // Corner-based: tile (gx, gy) owns the diamond from grid point (gx,gy) to (gx+1,gy+1)
    screenToGridX(screenX, screenY) {
        const sx = screenX - this.centerX - this.panX;
        const sy = screenY - this.centerY - this.panY;
        const tileSize = this.gridSize * this.scale;
        // Grid point formula: sx = (gx + gy) * tileSize/2, sy = (gx - gy) * tileSize/2
        // So: gx = (sx + sy) / tileSize, gy = (sx - sy) / tileSize
        // Floor to get the tile that owns this region
        return Math.floor((sx + sy) / tileSize);
    }

    // Convert screen coordinates to grid Y (inverse isometric projection)
    // Corner-based: tile (gx, gy) owns the diamond from grid point (gx,gy) to (gx+1,gy+1)
    screenToGridY(screenX, screenY) {
        const sx = screenX - this.centerX - this.panX;
        const sy = screenY - this.centerY - this.panY;
        const tileSize = this.gridSize * this.scale;
        return Math.floor((sx - sy) / tileSize);
    }

    screenToGrid(screenX, screenY) {
        const x = this.screenToGridX(screenX, screenY);
        const y = this.screenToGridY(screenX, screenY);
        return { x, y };
    }

    drawNetwork(network) {
        this.clear();
        this.drawGrid();

        if (!network || !network.tracks) {
            this.currentNetwork = null;
            return;
        }

        // Store network for hover redraws
        this.currentNetwork = network;

        // Store blocks and stations for rendering
        this.currentBlocks = network.blocks || {};
        this.currentStations = network.stations || [];
        // Create track lookup map for station rendering
        this.trackMap = {};
        network.tracks.forEach(track => {
            this.trackMap[`${track.x},${track.y}`] = track;
        });

        // Draw block ID colors BEFORE tracks (so they appear underneath)
        // Show if: debug checkbox is checked OR showBlockColorsOnly is set (block tool mode)
        const showBlockDebugElement = document.getElementById('debug-show-blocks');
        const showBlockDebug = showBlockDebugElement ? showBlockDebugElement.checked : false;
        if (showBlockDebug || this.showBlockColorsOnly) {
            this.blockHandler.drawBlockIdColors(network.tracks);
        }

        // Draw tracks (without signals first)
        network.tracks.forEach(track => {
            this.trackHandler.drawTrack(track, false);
        });

        // Draw grey dots at block boundaries AFTER tracks, so they appear on top
        // This creates visible gaps between different blocks
        if (network.blocks) {
            this.blockHandler.drawBlockBoundaryMarkers(network.tracks);
        }

        // Draw stations as spanning platforms (after tracks, so platforms appear on top)
        this.stationHandler.drawStations();

        // Draw signal-to-block connection lines (before signals, so they appear underneath)
        const showSignalDebugElement = document.getElementById('debug-show-signals');
        const showSignalDebug = showSignalDebugElement ? showSignalDebugElement.checked : false;
        if (showSignalDebug) {
            this.signalHandler.drawSignalBlockConnections(network.tracks);
        }

        // Draw signals on top of connection lines
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

        // Draw other debug overlays (after signals, before trains)
        this.debugHandler.drawDebugOverlays(network.tracks);

        // Draw block info labels and signal lines ONLY if debug checkbox is checked (not in block-tool-only mode)
        if (showBlockDebug && !this.showBlockColorsOnly) {
            this.blockHandler.drawBlockDetailLabels(network.tracks);
        }

        // Draw route overlays (reachable highlights, active routes, conflicts)
        this.routeHandler.drawRouteOverlays();

        // Draw trains
        if (network.trains) {
            network.trains.forEach(train => {
                this.trainHandler.drawTrain(train);
            });
            this.debugHandler.drawTrainPathDebug(network.trains);
        }
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
}
