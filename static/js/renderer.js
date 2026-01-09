// ============================================================================
// 6-bit Track Bits System (OpenTTD-style)
// Each bit represents one of 6 possible rail segments on a tile.
// ============================================================================

const TRACK_BITS = {
    NONE: 0,
    NS: 1,    // 0b000001 - North to South (vertical)
    EW: 2,    // 0b000010 - East to West (horizontal)
    NE: 4,    // 0b000100 - North to East (diagonal)
    NW: 8,    // 0b001000 - North to West (diagonal)
    SE: 16,   // 0b010000 - South to East (diagonal)
    SW: 32,   // 0b100000 - South to West (diagonal)
};

// Bit index (0-5) -> which two edges the segment connects
const BIT_TO_EDGES = {
    0: ['N', 'S'],  // TRACK_NS
    1: ['E', 'W'],  // TRACK_EW
    2: ['N', 'E'],  // TRACK_NE
    3: ['N', 'W'],  // TRACK_NW
    4: ['S', 'E'],  // TRACK_SE
    5: ['S', 'W'],  // TRACK_SW
};

// Edge masks - which bits touch each edge (for connectivity checks)
const EDGE_MASKS = { N: 13, E: 22, S: 49, W: 42 };

// Opposite edge lookup
const OPPOSITE_EDGE = { N: 'S', S: 'N', E: 'W', W: 'E' };

// Track rendering constants
const TRACK_WIDTH = 4;  // Base track width in pixels (at scale=1)
const DEBUG_BLOCK_WIDTH = 12;  // Debug block overlay width in pixels (at scale=1)

// Forbidden track_bits configurations (3-way switches and complex crossings)
const FORBIDDEN_TRACK_BITS = new Set([
    13, 22, 42, 49,        // 3-way switches
    28, 44, 52, 56, 60,    // Multi-diagonal without through route
    15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,  // 4-path configs
    31, 47, 55, 59, 61, 62, 63  // 5+ path configs
]);

// Convert track_bits to list of connected edges
function trackBitsToEdges(trackBits) {
    const edges = new Set();
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) {
            const [e1, e2] = BIT_TO_EDGES[bit];
            edges.add(e1);
            edges.add(e2);
        }
    }
    return edges;
}

// Get active segments as array of [edge1, edge2] pairs
function getActiveSegments(trackBits) {
    const segments = [];
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) {
            segments.push(BIT_TO_EDGES[bit]);
        }
    }
    return segments;
}

// Check if track_bits has a specific segment
function hasSegment(trackBits, edge1, edge2) {
    // Normalize to single-letter format
    const e1 = edge1.charAt(0).toUpperCase();
    const e2 = edge2.charAt(0).toUpperCase();

    for (let bit = 0; bit < 6; bit++) {
        const [a, b] = BIT_TO_EDGES[bit];
        if ((a === e1 && b === e2) || (a === e2 && b === e1)) {
            return !!(trackBits & (1 << bit));
        }
    }
    return false;
}

// Check if an edge is connected (has any segment touching it)
function edgeIsConnected(trackBits, edge) {
    const e = edge.charAt(0).toUpperCase();
    const mask = EDGE_MASKS[e];
    return mask ? !!(trackBits & mask) : false;
}

// Count number of active segments (popcount)
function countSegments(trackBits) {
    let count = 0;
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) count++;
    }
    return count;
}

// Check if track is a simple crossing (NS + EW)
function isCrossing(trackBits) {
    return trackBits === (TRACK_BITS.NS | TRACK_BITS.EW);  // 3
}

// Check if track is a diamond crossing (NE + SW or NW + SE)
function isDiamondCrossing(trackBits) {
    return trackBits === (TRACK_BITS.NE | TRACK_BITS.SW) ||  // 36
        trackBits === (TRACK_BITS.NW | TRACK_BITS.SE);    // 24
}

// Get opposite edge
function getOppositeEdge(edge) {
    const e = edge.charAt(0).toUpperCase();
    return OPPOSITE_EDGE[e] || null;
}

// Get segment group for smart line cap decisions
// Track segments are grouped by their orientation:
// - axial-vertical: NS (long vertical path)
// - axial-horizontal: EW (long horizontal path)
// - diagonal-nesw: NE/SW diagonal pair
// - diagonal-nwse: NW/SE diagonal pair
function getSegmentGroup(e1, e2) {
    const normalized = [e1, e2].sort().join('');
    if (normalized === 'NS') return 'axial-vertical';
    if (normalized === 'EW') return 'axial-horizontal';
    if (normalized === 'EN' || normalized === 'SW') return 'diagonal-nesw';
    if (normalized === 'ES' || normalized === 'NW') return 'diagonal-nwse';
    return 'unknown';
}

// ============================================================================
// Isometric Geometry Utility
// Centralized handling of all track segment and edge rotation math
// ============================================================================

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

        // Route overlay state
        this.reachableHighlights = new Set();  // Tile position strings "x,y" to highlight as reachable
        this.activeRoutes = [];                 // Route objects for visualization
        this.highlightedRouteId = null;         // Currently selected route for emphasis
        this.conflictFlashTiles = [];           // Tiles to flash red [[x,y], ...]
        this.conflictFlashEnd = 0;              // Timestamp when to stop flashing

        // Set up mouse tracking for hover effects
        this.canvas.addEventListener('mousemove', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const prevMouseX = this.mouseX;
            const prevMouseY = this.mouseY;
            this.mouseX = e.clientX - rect.left;
            this.mouseY = e.clientY - rect.top;

            // Redraw if mouse moved and we have a network
            // Need redraw for: block hover, station name hover, etc.
            const showBlocks = document.getElementById('debug-show-blocks')?.checked;
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
            const showBlocks = document.getElementById('debug-show-blocks')?.checked;
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
        this.ctx.fillStyle = '#0a0a0c';
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
        this.ctx.strokeStyle = '#2a2a34';
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

        this.ctx.strokeStyle = '#4a4a5a';
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
        this.ctx.fillStyle = '#8257e5';
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
        const showBlockDebug = document.getElementById('debug-show-blocks')?.checked;
        if (showBlockDebug || this.showBlockColorsOnly) {
            this.drawBlockIdColors(network.tracks);
        }

        // Draw tracks (without signals first)
        network.tracks.forEach(track => {
            this.drawTrack(track, false); // Skip signals in first pass
        });

        // Draw grey dots at block boundaries AFTER tracks, so they appear on top
        // This creates visible gaps between different blocks
        if (network.blocks) {
            this.drawBlockBoundaryMarkers(network.tracks);
        }

        // Draw stations as spanning platforms (after tracks, so platforms appear on top)
        this.drawStations();

        // Draw signal-to-block connection lines (before signals, so they appear underneath)
        const showSignalDebug = document.getElementById('debug-show-signals')?.checked;
        if (showSignalDebug) {
            this.drawSignalBlockConnections(network.tracks);
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
                    this.drawSingleSignal(signal, track.track_bits, half);
                }
                this.ctx.restore();
            }
        });

        // Draw other debug overlays (after signals, before trains)
        this.drawDebugOverlays(network.tracks);

        // Draw block info labels and signal lines ONLY if debug checkbox is checked (not in block-tool-only mode)
        if (showBlockDebug && !this.showBlockColorsOnly) {
            this.drawBlockDetailLabels(network.tracks);
        }

        // Draw route overlays (reachable highlights, active routes, conflicts)
        this.drawRouteOverlays();

        // Draw trains
        if (network.trains) {
            network.trains.forEach(train => {
                this.drawTrain(train);
            });
        }
    }

    drawTrack(track, drawSignals = true) {
        // Convert grid coordinates to screen coordinates (isometric)
        const cellSize = this.gridSize * this.scale;
        const cx = this.gridToScreenX(track.x, track.y);  // center X
        const cy = this.gridToScreenY(track.x, track.y);   // center Y
        const half = cellSize / 2;

        // Get track_bits directly (6-bit system is the source of truth)
        const trackBits = track.track_bits ?? 0;

        this.ctx.save();
        this.ctx.translate(cx, cy);

        // ATC style: determine track color based on block state
        // Color scheme: FREE=Blue, RESERVED=Green, OCCUPIED=Red, CAUTION=Yellow
        let trackColor = '#666'; // Default grey for tracks without blocks
        if (track.block_section_id && this.currentBlocks) {
            const block = this.currentBlocks[track.block_section_id];
            if (block) {
                const blockState = (block.state || 'free').toLowerCase();
                if (blockState === 'occupied') {
                    trackColor = '#e83f5b'; // Red for occupied (train in block)
                } else if (blockState === 'reserved') {
                    trackColor = '#04d361'; // Green for reserved (train approaching)
                } else if (blockState === 'free') {
                    trackColor = '#4285f4'; // Blue for free (safe to enter)
                } else if (blockState === 'caution') {
                    trackColor = '#ffcd1e'; // Yellow for caution (train just left, slow down)
                }
            }
        }
        // Legacy fallback: direct track properties
        else if (track.occupied_by) {
            trackColor = '#e83f5b'; // Red if occupied
        } else if (track.reserved_by) {
            trackColor = '#ffcd1e'; // Yellow if reserved
        }

        // Get switch info from track (new: single switch_state per tile)
        const isSwitch = track.is_switch || false;
        const switchState = track.switch_state || 0;  // 0=MAIN, 1=DIVERGE
        const isDiagonalOnly = track.is_diagonal_only_switch || false;
        const switchPoints = track.switch_points || {};

        // Render based on track configuration
        if (isSwitch) {
            // Track has switch points - render with active/inactive paths
            this.drawTrackWithSwitches(trackBits, half, trackColor, switchState, isDiagonalOnly, switchPoints);
            
            // Draw brown ring around switch indicator if locked by a route
            if (track.locked_by_route) {
                this.ctx.strokeStyle = '#8B4513';  // SaddleBrown
                this.ctx.lineWidth = 2 * this.scale;
                this.ctx.beginPath();
                this.ctx.arc(0, 0, 7 * this.scale, 0, Math.PI * 2);
                this.ctx.stroke();
            }
        } else {
            // Simple track - render all segments
            this.drawTrackFromBits(trackBits, half, trackColor, track.dead_end_ports || []);
        }

        // Draw signals (stations are drawn separately in drawStations() as spanning platforms)
        // Now uses track.signals array (enriched by backend) for multi-signal support
        // Signals can be drawn separately for better layering control
        if (drawSignals && track.signals && track.signals.length > 0) {
            for (const signal of track.signals) {
                this.drawSingleSignal(signal, track.track_bits, half);
            }
        }

        this.ctx.restore();

        // Always draw dead-end highlights (permanent feature)
        if (track.dead_end_ports && track.dead_end_ports.length > 0) {
            this.highlightDeadEnds(track.x, track.y, track.dead_end_ports, track.track_bits || 0);
        }
    }

    // Get isometric edge midpoints for the diamond tile
    // Each edge connects to a neighbor tile in that cardinal direction.
    //
    // Diamond vertices (named by visual screen position, Y increases downward):
    //   top(0, -h), right(h, 0), bottom(0, h), left(-h, 0)
    //
    // Edge midpoints (between adjacent vertices):
    //   N edge (y+1 neighbor, up-right on screen): between top & right → (h/2, -h/2)
    //   E edge (x+1 neighbor, down-right on screen): between right & bottom → (h/2, h/2)
    //   S edge (y-1 neighbor, down-left on screen): between bottom & left → (-h/2, h/2)
    //   W edge (x-1 neighbor, up-left on screen): between left & top → (-h/2, -h/2)
    getEdgePoints(half) {
        const h = half;  // half of tileSize
        return {
            'N': { x: h / 2, y: -h / 2 },   // top-right edge midpoint (Y+ neighbor)
            'E': { x: h / 2, y: h / 2 },    // bottom-right edge midpoint (X+ neighbor)
            'S': { x: -h / 2, y: h / 2 },   // bottom-left edge midpoint (Y- neighbor)
            'W': { x: -h / 2, y: -h / 2 }   // top-left edge midpoint (X- neighbor)
        };
    }

    /**
     * Draw a filled polygon segment along a track path in the isometric grid.
     * 
     * Width parameter controls offset along grid lines (perpendicular to edges).
     * Each edge has its own perpendicular direction (positive = counter-clockwise).
     * 
     * @param {Object} start - Start edge midpoint {x, y} in tile-local coords
     * @param {Object} end - End edge midpoint {x, y} in tile-local coords
     * @param {number} width - Width along grid lines (perpendicular to edges)
     * @param {string} e1 - Start edge name ('N', 'E', 'S', 'W')
     * @param {string} e2 - End edge name ('N', 'E', 'S', 'W')
     * @param {string} fillColor - Fill color for the polygon
     */
    drawGridSegment(start, end, width, e1, e2, fillColor) {
        const halfWidth = width / 2;


        // Get perpendicular offset for each edge
        // Positive direction is counter-clockwise around the tile
        const getEdgeOffset = (edge) => {
            switch (edge) {
                case 'N': return { x: -halfWidth, y: -halfWidth };
                case 'E': return { x: halfWidth, y: -halfWidth };
                case 'S': return { x: halfWidth, y: halfWidth };
                case 'W': return { x: -halfWidth, y: halfWidth };
                default: return { x: 0, y: 0 };
            }
        };

        const offset1 = getEdgeOffset(e1);
        const offset2 = getEdgeOffset(e2);

        // Create 4 corner points
        // p1, p2 on CCW side; p3, p4 on CW side
        const p1 = { x: start.x + offset1.x, y: start.y + offset1.y };  // Start + offset (CCW)
        const p2 = { x: start.x - offset1.x, y: start.y - offset1.y };  // Start - offset (CW)
        const p3 = { x: end.x + offset2.x, y: end.y + offset2.y };      // End + offset (CCW)
        const p4 = { x: end.x - offset2.x, y: end.y - offset2.y };      // End - offset (CW)



        // Draw filled polygon in order around perimeter
        this.ctx.fillStyle = fillColor;
        this.ctx.beginPath();
        this.ctx.moveTo(p1.x, p1.y);
        this.ctx.lineTo(p2.x, p2.y);
        this.ctx.lineTo(p3.x, p3.y);
        this.ctx.lineTo(p4.x, p4.y);
        this.ctx.closePath();
        this.ctx.fill();
    }

    // Draw track directly from 6-bit track_bits
    drawTrackFromBits(trackBits, half, color, deadEndPorts = []) {
        const edgePoints = this.getEdgePoints(half);

        // Draw each active segment using filled polygons
        const segments = getActiveSegments(trackBits);
        for (const [e1, e2] of segments) {
            const start = edgePoints[e1];
            const end = edgePoints[e2];
            if (start && end) {
                this.drawGridSegment(start, end, TRACK_WIDTH * this.scale, e1, e2, color);
            }
        }

        // Draw dead-end paths (no dots - dead-end highlights are drawn separately)
        // For each dead-end port, find which segment it belongs to and render the path
        for (const port of deadEndPorts) {
            const deadEndEdge = port.charAt(0).toUpperCase();

            // Find the segment that uses this dead-end edge
            for (const [e1, e2] of segments) {
                if (e1 === deadEndEdge || e2 === deadEndEdge) {
                    const otherEdge = (e1 === deadEndEdge) ? e2 : e1;
                    const deadEndPoint = edgePoints[deadEndEdge];
                    const otherPoint = edgePoints[otherEdge];

                    // Check if this is an axial path (crosses center) or diagonal
                    const isAxial = (deadEndEdge === 'N' && otherEdge === 'S') ||
                        (deadEndEdge === 'S' && otherEdge === 'N') ||
                        (deadEndEdge === 'E' && otherEdge === 'W') ||
                        (deadEndEdge === 'W' && otherEdge === 'E');

                    if (isAxial) {
                        // Axial: draw from center to opposite edge only
                        this.drawGridSegment({ x: 0, y: 0 }, otherPoint, TRACK_WIDTH * this.scale, deadEndEdge, otherEdge, color);
                    } else {
                        // Diagonal: draw from one edge to other
                        this.drawGridSegment(deadEndPoint, otherPoint, TRACK_WIDTH * this.scale, e1, e2, color);
                    }
                    break; // Found the segment for this dead-end
                }
            }
        }

        // Handle empty track (stub point)
        if (trackBits === 0 && deadEndPorts.length === 0) {
            this.ctx.fillStyle = color;
            this.ctx.beginPath();
            this.ctx.arc(0, 0, 5 * this.scale, 0, Math.PI * 2);
            this.ctx.fill();

            this.ctx.strokeStyle = '#222';
            this.ctx.lineWidth = 2 * this.scale;
            this.ctx.beginPath();
            this.ctx.arc(0, 0, 3 * this.scale, 0, Math.PI * 2);
            this.ctx.stroke();
        }

        // Draw crossing indicator for multi-segment crossings
        if (isCrossing(trackBits)) {
            this.ctx.fillStyle = '#8257e5';
            this.ctx.beginPath();
            this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
            this.ctx.fill();
        }
    }

    // Draw track with switches (synchronized tile-level state)
    // switchState: 0=MAIN, 1=DIVERGE
    // isDiagonalOnly: true if no NS/EW segments (uses clockwise ordering instead)
    // switchPoints: {entryEdge: [exit1, exit2, ...]} from backend
    drawTrackWithSwitches(trackBits, half, color, switchState, isDiagonalOnly, switchPoints) {
        const edgePoints = this.getEdgePoints(half);

        // Segment definitions
        const SEGMENTS = [
            { bit: 0, edges: ['N', 'S'], name: 'NS' },  // Long
            { bit: 1, edges: ['E', 'W'], name: 'EW' },  // Long
            { bit: 2, edges: ['N', 'E'], name: 'NE' },  // Short
            { bit: 3, edges: ['N', 'W'], name: 'NW' },  // Short
            { bit: 4, edges: ['S', 'E'], name: 'SE' },  // Short
            { bit: 5, edges: ['S', 'W'], name: 'SW' },  // Short
        ];

        // For diagonal-only switches, compute active segments using clockwise ordering
        let activeSegmentBits = new Set();

        if (isDiagonalOnly && switchPoints) {
            // Clockwise order: N, E, S, W
            const CLOCKWISE = ['N', 'E', 'S', 'W'];

            // For each switch point entry, determine which exit is active
            for (const [entryEdge, exits] of Object.entries(switchPoints)) {
                if (!exits || exits.length < 2) continue;

                // Sort exits by clockwise order
                const sortedExits = [...exits].sort((a, b) =>
                    CLOCKWISE.indexOf(a) - CLOCKWISE.indexOf(b)
                );

                // MAIN (0) picks first clockwise, DIVERGE (1) picks second
                const activeExit = switchState === 0 ? sortedExits[0] : sortedExits[1];

                // Find the segment connecting entryEdge to activeExit
                for (const seg of SEGMENTS) {
                    if (!(trackBits & (1 << seg.bit))) continue;
                    const [e1, e2] = seg.edges;
                    if ((e1 === entryEdge && e2 === activeExit) ||
                        (e2 === entryEdge && e1 === activeExit)) {
                        activeSegmentBits.add(seg.bit);
                    }
                }
            }
        }

        // Collect all segments present on this tile for smart line cap detection
        const allSegments = SEGMENTS.filter(seg => trackBits & (1 << seg.bit))
            .map(seg => seg.edges);

        // Draw all segments with appropriate styling
        // Pass 1: Draw inactive segments
        for (const seg of SEGMENTS) {
            if (!(trackBits & (1 << seg.bit))) continue;  // Skip segments not in track

            const [e1, e2] = seg.edges;
            const start = edgePoints[e1];
            const end = edgePoints[e2];
            if (!start || !end) continue;

            let isActive;
            if (isDiagonalOnly) {
                // For diagonal-only, use computed active segments
                isActive = activeSegmentBits.has(seg.bit);
            } else {
                // Standard logic: long segments (bit 0-1) active on MAIN, short on DIVERGE
                const isLongSegment = seg.bit <= 1;
                isActive = (isLongSegment && switchState === 0) ||
                    (!isLongSegment && switchState === 1);
            }

            // Draw inactive segments only in first pass
            if (!isActive) {
                const segmentColor = '#444';
                const segmentWidth = 2 * this.scale;
                this.drawGridSegment(start, end, segmentWidth, e1, e2, segmentColor);
            }
        }

        // Pass 2: Draw active segments on top
        for (const seg of SEGMENTS) {
            if (!(trackBits & (1 << seg.bit))) continue;  // Skip segments not in track

            const [e1, e2] = seg.edges;
            const start = edgePoints[e1];
            const end = edgePoints[e2];
            if (!start || !end) continue;

            let isActive;
            if (isDiagonalOnly) {
                // For diagonal-only, use computed active segments
                isActive = activeSegmentBits.has(seg.bit);
            } else {
                // Standard logic: long segments (bit 0-1) active on MAIN, short on DIVERGE
                const isLongSegment = seg.bit <= 1;
                isActive = (isLongSegment && switchState === 0) ||
                    (!isLongSegment && switchState === 1);
            }

            // Draw active segments only in second pass
            if (isActive) {
                const segmentColor = color;
                const segmentWidth = TRACK_WIDTH * this.scale;
                this.drawGridSegment(start, end, segmentWidth, e1, e2, segmentColor);
            }
        }

        // Draw switch indicator at tile center
        const isMain = switchState === 0;
        this.ctx.fillStyle = isMain ? '#04d361' : '#ffd93d';
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 5 * this.scale, 0, Math.PI * 2);
        this.ctx.fill();

        this.ctx.fillStyle = '#000';
        this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(isMain ? 'M' : 'D', 0, 0);

        // Add clockwise indicator for diagonal-only switches
        if (isDiagonalOnly) {
            this.ctx.font = `${7 * this.scale}px sans-serif`;
            this.ctx.fillStyle = '#ff9900';
            this.ctx.fillText('⟳', 9 * this.scale, -7 * this.scale);
        }
    }

    drawSingleSignal(signal, trackBits, half) {
        // Get directions and calculate positioning
        // approach_direction = where trains come FROM (signal light faces this direction)
        // detection_direction = where trains travel TO (direction signal monitors)
        // Both directions are REQUIRED - fail visibly if missing
        if (!signal.detection_direction) {
            console.error(`[Renderer] Signal ${signal.signal_id} missing detection_direction!`, signal);
            return;  // Don't render invalid signal
        }
        if (!signal.approach_direction) {
            console.error(`[Renderer] Signal ${signal.signal_id} missing approach_direction!`, signal);
            return;  // Don't render invalid signal
        }
        const approachDir = signal.approach_direction;
        const detectionDir = signal.detection_direction;
        const facingEdge = approachDir.charAt(0).toUpperCase();  // Direction train comes from
        const edgePoints = this.getEdgePoints(half);
        const isLongPath = (trackBits === 1 || trackBits === 2);  // NS or EW straight tracks

        // Detect diagonal tracks: NE(4), NW(8), SE(16), SW(32)
        const isDiagonal = (trackBits === 4 || trackBits === 8 || trackBits === 16 || trackBits === 32);

        // Calculate signal position based on path type
        let sx, sy;
        if (isLongPath) {
            // Right-hand side rule: position on right side relative to train's TRAVEL direction
            // detection_direction IS the travel direction (where trains go TO)
            const travelEdge = detectionDir.charAt(0).toUpperCase();
            // Train going N -> right side is E
            // Train going S -> right side is W
            // Train going E -> right side is S
            // Train going W -> right side is N
            const rightSide = {
                'N': 'E', 'S': 'W', 'E': 'S', 'W': 'N'
            }[travelEdge] || 'E';
            const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
            sx = sidePoint.x * 0.5;  // Quarter point toward that side
            sy = sidePoint.y * 0.5;
        } else {
            // Center for short paths (diagonals: NE, NW, SE, SW)
            sx = 0;
            sy = 0;
        }

        // Get signal color based on state
        const state = (signal.state || 'green').toUpperCase();
        let signalColor;
        if (state === 'GREEN') {
            signalColor = '#04d361';
        } else if (state === 'YELLOW') {
            signalColor = '#ffd93d';
        } else {
            signalColor = '#e83f5b';  // RED or unknown
        }

        // Calculate direction for stick angle
        let dirX, dirY;

        if (isDiagonal) {
            // For diagonal tracks, stick points ALONG the diagonal path (45 degree angle in isometric)
            //
            // IMPORTANT: Cannot use approach_direction from backend for diagonals!
            // Backend computes geometric opposite (N→S, E→W), but diagonals only connect two
            // adjacent edges (e.g., NE connects N and E, not N and S).
            //
            // Solution: Use detection_direction to determine which edge train exits from,
            // then point stick toward the OTHER edge of the diagonal (where train comes from).
            //
            // Base diagonal vectors represent direction from edge2 toward edge1 of the track
            // These are calculated from isometric edge positions:
            //   N edge: (h/2, -h/2)  top-right    |  E edge: (h/2, h/2)   bottom-right
            //   S edge: (-h/2, h/2)  bottom-left  |  W edge: (-h/2, -h/2) top-left
            //
            // Vector calculation (edge2 → edge1):
            //   NE (4):  E(h/2, h/2) → N(h/2, -h/2) = (0, -h)  → normalized (0, -1)  vertical up
            //   NW (8):  W(-h/2,-h/2) → N(h/2, -h/2) = (h, 0)  → normalized (1, 0)   horizontal right
            //   SE (16): E(h/2, h/2) → S(-h/2, h/2) = (-h, 0)  → normalized (-1, 0)  horizontal left
            //   SW (32): W(-h/2,-h/2) → S(-h/2, h/2) = (0, h)  → normalized (0, 1)   vertical down
            let diagonalX, diagonalY;
            switch (trackBits) {
                case 4: diagonalX = 0; diagonalY = -1; break;  // NE: vertical up (E→N)
                case 8: diagonalX = 1; diagonalY = 0; break;  // NW: horizontal right (W→N)
                case 16: diagonalX = -1; diagonalY = 0; break;  // SE: horizontal left (E→S)
                case 32: diagonalX = 0; diagonalY = 1; break;  // SW: vertical down (W→S)
                default: diagonalX = 1; diagonalY = 0; break;
            }

            // Determine the two edges of this diagonal
            let edge1, edge2;
            switch (trackBits) {
                case 4: edge1 = 'N'; edge2 = 'E'; break;
                case 8: edge1 = 'N'; edge2 = 'W'; break;
                case 16: edge1 = 'S'; edge2 = 'E'; break;
                case 32: edge1 = 'S'; edge2 = 'W'; break;
                default: edge1 = 'N'; edge2 = 'E'; break;
            }

            // Use detection direction (not approach) to determine which way to point
            // If train exits via edge1, it approaches from edge2 → flip base to point toward edge2
            const detectionEdge = detectionDir.charAt(0).toUpperCase();
            if (detectionEdge === edge1) {
                diagonalX = -diagonalX;
                diagonalY = -diagonalY;
            }

            // Normalize
            const len = Math.sqrt(diagonalX * diagonalX + diagonalY * diagonalY);
            dirX = diagonalX / len;
            dirY = diagonalY / len;
        } else {
            // For straight tracks, use cardinal direction
            const travelEdge = facingEdge || 'E';
            const dirPoint = edgePoints[travelEdge] || { x: 1, y: 0 };
            const len = Math.sqrt(dirPoint.x * dirPoint.x + dirPoint.y * dirPoint.y);
            dirX = dirPoint.x / len;
            dirY = dirPoint.y / len;
        }

        // Draw black stick parallel to path direction
        const stickLength = half * 0.4;
        const stickEndX = sx + dirX * stickLength;
        const stickEndY = sy + dirY * stickLength;

        // Draw grey outer circle
        this.ctx.strokeStyle = '#999';
        this.ctx.lineWidth = 1.5 * this.scale;
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, 4 * this.scale, 0, Math.PI * 2);
        this.ctx.stroke();

        this.ctx.strokeStyle = '#999';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        this.ctx.moveTo(sx, sy);
        this.ctx.lineTo(stickEndX, stickEndY);
        this.ctx.stroke();

        // Draw colored inner circle
        const innerRadius = 3 * this.scale;
        this.ctx.fillStyle = signalColor;
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, innerRadius, 0, Math.PI * 2);
        this.ctx.fill();



        // Draw orthogonal stroke at end of stick
        const orthoX = -dirY;  // Perpendicular vector
        const orthoY = dirX;
        const orthoLen = half * 0.15;

        this.ctx.beginPath();
        this.ctx.moveTo(stickEndX - orthoX * orthoLen, stickEndY - orthoY * orthoLen);
        this.ctx.lineTo(stickEndX + orthoX * orthoLen, stickEndY + orthoY * orthoLen);
        this.ctx.stroke();

        // Draw signal name label if set and "Show Signal Details" debug is enabled
        const showSignalDetails = document.getElementById('debug-show-signals')?.checked;
        if (signal.name && showSignalDetails) {
            this.ctx.fillStyle = '#fff';
            this.ctx.font = `bold ${6 * this.scale}px sans-serif`;
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'top';
            // Position label below signal circle
            const labelY = sy + 8 * this.scale;
            this.ctx.fillText(signal.name, sx, labelY);
        }

        // Draw orange warning triangle for invalid signals (no monitored block configured)
        // A signal is invalid if it's a BLOCK type signal but has no monitored_block_id
        const signalType = signal.signal_type || 'block';
        const hasBlockConfigured = signal.monitored_block_id;
        if (signalType === 'block' && !hasBlockConfigured) {
            // Draw orange warning triangle
            const triSize = 5 * this.scale;
            const triX = sx + 6 * this.scale;  // Offset to right of signal
            const triY = sy - 6 * this.scale;  // Offset above signal

            this.ctx.fillStyle = '#ff9500';  // Orange warning color
            this.ctx.beginPath();
            this.ctx.moveTo(triX, triY - triSize);           // Top point
            this.ctx.lineTo(triX - triSize * 0.866, triY + triSize * 0.5);  // Bottom left
            this.ctx.lineTo(triX + triSize * 0.866, triY + triSize * 0.5);  // Bottom right
            this.ctx.closePath();
            this.ctx.fill();

            // Draw exclamation mark inside triangle
            this.ctx.fillStyle = '#000';
            this.ctx.font = `bold ${6 * this.scale}px sans-serif`;
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText('!', triX, triY);
        }
    }

    /**
     * Draw all stations as spanning platforms.
     * Each station is rendered as a single platform rectangle parallel to the track,
     * spanning all tiles of the station.
     *
     * Algorithm:
     * 1. Find the two endpoint tiles (largest distance apart)
     * 2. Calculate path endpoints on those tiles (4 points total)
     * 3. Find the longest pair - this defines platform direction
     * 4. Draw platform rectangle parallel to that line, offset by side
     */
    drawStations() {
        if (!this.currentStations || this.currentStations.length === 0) return;

        const tileSize = this.gridSize * this.scale;
        const half = tileSize / 2;

        for (const station of this.currentStations) {
            if (!station.positions || station.positions.length === 0) continue;

            const positions = station.positions;
            if (positions.length === 0) continue;

            let orientation = station.orientation;
            let platformSides = station.platform_sides || [];
            const stationName = station.name || station.station_id || 'Station';

            // Fallback: if no orientation from backend, try to determine from track
            if (!orientation && positions.length > 0) {
                const firstPos = positions[0];
                const firstTrack = this.trackMap[`${firstPos[0]},${firstPos[1]}`];
                if (firstTrack) {
                    const trackBits = firstTrack.track_bits || 0;
                    if (trackBits & TRACK_BITS.NS) orientation = 'NS';
                    else if (trackBits & TRACK_BITS.EW) orientation = 'EW';
                    else if (trackBits & TRACK_BITS.NE || trackBits & TRACK_BITS.SW) orientation = 'NE_SW';
                    else if (trackBits & TRACK_BITS.NW || trackBits & TRACK_BITS.SE) orientation = 'NW_SE';
                }
            }

            // Fallback: if no platformSides, use default based on platform_side
            if (platformSides.length === 0 && orientation) {
                const sideMapping = {
                    'NS': { 'both': ['west', 'east'], 'left': ['west'], 'right': ['east'] },
                    'EW': { 'both': ['north', 'south'], 'left': ['north'], 'right': ['south'] },
                    'NE_SW': { 'both': ['northwest', 'southeast'], 'left': ['northwest'], 'right': ['southeast'] },
                    'NW_SE': { 'both': ['southwest', 'northeast'], 'left': ['southwest'], 'right': ['northeast'] }
                };
                const side = station.platform_side || 'both';
                platformSides = sideMapping[orientation]?.[side] || ['west', 'east'];
            }

            // Find the two endpoint tiles (largest distance apart)
            const endpointTiles = this.findEndpointTiles(positions);
            if (!endpointTiles) continue;

            const [tile1, tile2] = endpointTiles;

            // Get track info for each endpoint tile
            const track1 = this.trackMap[`${tile1[0]},${tile1[1]}`];
            const track2 = this.trackMap[`${tile2[0]},${tile2[1]}`];
            if (!track1 || !track2) continue;

            // Get path endpoints for each tile (where track enters/exits)
            const endpoints1 = this.getPathEndpoints(tile1, track1.track_bits || 0, half);
            const endpoints2 = this.getPathEndpoints(tile2, track2.track_bits || 0, half);

            // Find the longest pair among all 4 endpoints
            const longestPair = this.findLongestPair([...endpoints1, ...endpoints2]);
            if (!longestPair) continue;

            // Draw platform
            this.ctx.save();

            if (orientation && platformSides.length > 0) {
                this.drawOrientedPlatform(
                    longestPair[0], longestPair[1], orientation, platformSides,
                    10 * this.scale, half
                );
            }

            // Draw station name label alongside the platform
            this.drawStationLabel(
                stationName,
                longestPair[0], longestPair[1],
                orientation, platformSides[0],
                half
            );

            this.ctx.restore();
        }
    }

    /**
     * Draw block boundary separators for multi-block stations.
     * Shows visual dividers between tiles assigned to different blocks.
     */
    /**
     * Get color for block state
     */
    getBlockStateColor(state) {
        const colors = {
            'free': '#00ff88',
            'reserved': '#ffd93d',
            'occupied': '#e83f5b',
            'caution': '#ff8c42'
        };
        return colors[state] || '#888';
    }

    /**
     * Generate abbreviated station name (like real-world ATC codes).
     * Examples: "Central Station" -> "CNT", "New York Penn" -> "NYP"
     */
    abbreviateStationName(name) {
        if (!name || name.length <= 4) return name.toUpperCase();

        // Split into words and take first letter of each (up to 4)
        const words = name.split(/[\s\-_]+/).filter(w => w.length > 0);
        if (words.length >= 2) {
            // Multiple words: take first letter of each (max 4)
            return words.slice(0, 4).map(w => w[0].toUpperCase()).join('');
        } else {
            // Single word: take first 3-4 consonants or first 4 chars
            const consonants = name.replace(/[aeiou]/gi, '').toUpperCase();
            if (consonants.length >= 3) {
                return consonants.slice(0, 4);
            }
            return name.slice(0, 4).toUpperCase();
        }
    }

    /**
     * Draw station label alongside the platform.
     * Shows abbreviated name by default, full name on hover.
     */
    drawStationLabel(fullName, point1, point2, orientation, side, half) {
        const abbrev = this.abbreviateStationName(fullName);

        // Calculate label position: center of platform, offset to the side
        const centerX = (point1.x + point2.x) / 2;
        const centerY = (point1.y + point2.y) / 2;

        // Calculate direction along platform
        const dx = point2.x - point1.x;
        const dy = point2.y - point1.y;
        const length = Math.sqrt(dx * dx + dy * dy);
        if (length === 0) return;

        // Perpendicular direction (same as platform offset)
        const perpX = -dy / length;
        const perpY = dx / length;

        // Get side multiplier to position label on the correct side
        const sideMultiplier = this.getSideMultiplier(side, orientation);

        // Offset label to be alongside the platform (past the platform width)
        const labelOffset = half * 0.3 + 18 * this.scale; // Past the platform
        const labelX = centerX + perpX * labelOffset * sideMultiplier;
        const labelY = centerY + perpY * labelOffset * sideMultiplier;

        // Check if mouse is hovering near this station
        const hoverRadius = 30 * this.scale;
        const distToMouse = Math.sqrt(
            Math.pow(this.mouseX - centerX, 2) +
            Math.pow(this.mouseY - centerY, 2)
        );
        const isHovered = distToMouse < hoverRadius + length / 2;

        // Choose display text based on hover state
        const displayText = isHovered ? fullName : abbrev;

        // Font size: smaller for abbreviation, slightly larger for full name on hover
        const fontSize = isHovered ? 9 * this.scale : 7 * this.scale;
        this.ctx.font = `bold ${fontSize}px sans-serif`;

        const textWidth = this.ctx.measureText(displayText).width;
        const labelPadding = 3 * this.scale;
        const labelHeight = (isHovered ? 12 : 10) * this.scale;

        // Background: more transparent for abbreviation, more visible on hover
        const bgAlpha = isHovered ? 0.8 : 0.5;
        this.ctx.fillStyle = `rgba(0, 0, 0, ${bgAlpha})`;
        this.ctx.fillRect(
            labelX - textWidth / 2 - labelPadding,
            labelY - labelHeight / 2,
            textWidth + labelPadding * 2,
            labelHeight
        );

        // Border for visibility
        this.ctx.strokeStyle = isHovered ? 'rgba(255, 255, 255, 0.6)' : 'rgba(255, 255, 255, 0.3)';
        this.ctx.lineWidth = 1;
        this.ctx.strokeRect(
            labelX - textWidth / 2 - labelPadding,
            labelY - labelHeight / 2,
            textWidth + labelPadding * 2,
            labelHeight
        );

        // Text
        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(displayText, labelX, labelY);
    }

    /**
     * Find the two tiles that are furthest apart (endpoint tiles).
     */
    findEndpointTiles(positions) {
        if (positions.length === 0) return null;
        if (positions.length === 1) return [positions[0], positions[0]];

        let maxDist = -1;
        let endpointPair = null;

        for (let i = 0; i < positions.length; i++) {
            for (let j = i + 1; j < positions.length; j++) {
                const dx = positions[j][0] - positions[i][0];
                const dy = positions[j][1] - positions[i][1];
                const dist = dx * dx + dy * dy;
                if (dist > maxDist) {
                    maxDist = dist;
                    endpointPair = [positions[i], positions[j]];
                }
            }
        }

        return endpointPair;
    }

    /**
     * Get the path endpoints for a tile based on its track_bits.
     * Returns 2 screen coordinate points where the track enters/exits the tile.
     */
    getPathEndpoints(tilePos, trackBits, half) {
        const centerX = this.gridToScreenX(tilePos[0], tilePos[1]);
        const centerY = this.gridToScreenY(tilePos[0], tilePos[1]);

        // Determine which edges the track connects
        // In isometric view:
        //   N edge = top-right (centerX + half/2, centerY - half/2)
        //   S edge = bottom-left (centerX - half/2, centerY + half/2)
        //   E edge = bottom-right (centerX + half/2, centerY + half/2)
        //   W edge = top-left (centerX - half/2, centerY - half/2)
        const edgePositions = {
            'N': { x: centerX + half / 2, y: centerY - half / 2 },
            'S': { x: centerX - half / 2, y: centerY + half / 2 },
            'E': { x: centerX + half / 2, y: centerY + half / 2 },
            'W': { x: centerX - half / 2, y: centerY - half / 2 }
        };

        const endpoints = [];

        // Check each track segment and add its edge endpoints
        if (trackBits & TRACK_BITS.NS) {
            endpoints.push(edgePositions['N'], edgePositions['S']);
        }
        if (trackBits & TRACK_BITS.EW) {
            endpoints.push(edgePositions['E'], edgePositions['W']);
        }
        if (trackBits & TRACK_BITS.NE) {
            endpoints.push(edgePositions['N'], edgePositions['E']);
        }
        if (trackBits & TRACK_BITS.NW) {
            endpoints.push(edgePositions['N'], edgePositions['W']);
        }
        if (trackBits & TRACK_BITS.SE) {
            endpoints.push(edgePositions['S'], edgePositions['E']);
        }
        if (trackBits & TRACK_BITS.SW) {
            endpoints.push(edgePositions['S'], edgePositions['W']);
        }

        // Return first 2 endpoints (for simple track, this is correct)
        // For complex tracks, we might need smarter logic
        return endpoints.slice(0, 2);
    }

    /**
     * Find the pair of points with the longest distance.
     */
    findLongestPair(points) {
        if (points.length < 2) return null;

        let maxDist = -1;
        let longestPair = null;

        for (let i = 0; i < points.length; i++) {
            for (let j = i + 1; j < points.length; j++) {
                const dx = points[j].x - points[i].x;
                const dy = points[j].y - points[i].y;
                const dist = dx * dx + dy * dy;
                if (dist > maxDist) {
                    maxDist = dist;
                    longestPair = [points[i], points[j]];
                }
            }
        }

        return longestPair;
    }

    /**
     * Draw platform as a rectangle parallel to the line between two endpoints.
     * The platform is offset perpendicular to the line based on side.
     *
     * @param {Object} point1 - {x, y} screen coordinates of first endpoint
     * @param {Object} point2 - {x, y} screen coordinates of second endpoint
     * @param {string} orientation - 'NS', 'EW', 'NE_SW', or 'NW_SE'
     * @param {Array} platformSides - Compass directions like ['west', 'east']
     * @param {number} width - Platform width in pixels
     * @param {number} half - Half tile size
     */
    drawOrientedPlatform(point1, point2, orientation, platformSides, width, half) {
        // Platform color
        this.ctx.fillStyle = '#5a5a7a';
        this.ctx.strokeStyle = '#3a3a5a';
        this.ctx.lineWidth = 1 * this.scale;

        // Normalize point order based on orientation to ensure consistent direction
        // This prevents the platform from flipping when endpoints change
        let p1 = point1;
        let p2 = point2;

        // Determine if we need to swap points to maintain consistent direction
        // For each orientation, ensure the direction vector follows the expected direction
        const needsSwap = this.shouldSwapEndpoints(p1, p2, orientation);
        if (needsSwap) {
            [p1, p2] = [p2, p1];
        }

        // Calculate direction vector along the platform
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;
        const length = Math.sqrt(dx * dx + dy * dy);
        if (length === 0) return;

        // Normalized direction and perpendicular
        const dirX = dx / length;
        const dirY = dy / length;
        // Perpendicular: rotate 90 degrees (perpX, perpY) = (-dirY, dirX)
        const perpX = -dirY;
        const perpY = dirX;

        // Offset distance from track center
        const perpOffset = half * 0.3;

        // Draw each platform side
        for (const side of platformSides) {
            const sideMultiplier = this.getSideMultiplier(side, orientation);

            // Offset from the track line
            const offsetX = perpX * perpOffset * sideMultiplier;
            const offsetY = perpY * perpOffset * sideMultiplier;

            // Inner edge (closer to track)
            const innerStartX = point1.x + offsetX;
            const innerStartY = point1.y + offsetY;
            const innerEndX = point2.x + offsetX;
            const innerEndY = point2.y + offsetY;

            // Outer edge (further from track)
            const outerStartX = innerStartX + perpX * width * sideMultiplier;
            const outerStartY = innerStartY + perpY * width * sideMultiplier;
            const outerEndX = innerEndX + perpX * width * sideMultiplier;
            const outerEndY = innerEndY + perpY * width * sideMultiplier;

            this.drawPlatformQuad(
                innerStartX, innerStartY,
                outerStartX, outerStartY,
                outerEndX, outerEndY,
                innerEndX, innerEndY
            );
        }
    }

    /**
     * Get multiplier for platform side offset based on compass direction.
     * The perpendicular in drawOrientedPlatform is calculated as (-dirY, dirX),
     * which is a counter-clockwise rotation from the direction vector.
     */
    getSideMultiplier(side, orientation) {
        // Map compass direction to +1 or -1 multiplier for perpendicular offset
        // +1 = use perpendicular direction as-is
        // -1 = use opposite of perpendicular direction
        const sideMap = {
            // NS orientation: perpendicular points toward east when going N->S
            'NS': { 'east': 1, 'west': -1 },
            // EW orientation: perpendicular points toward south when going E->W
            'EW': { 'south': 1, 'north': -1 },
            // NE_SW orientation: perpendicular points toward southeast when going NE->SW
            'NE_SW': { 'southeast': 1, 'northwest': -1 },
            // NW_SE orientation: perpendicular points toward northeast when going NW->SE
            'NW_SE': { 'northeast': 1, 'southwest': -1 }
        };

        return sideMap[orientation]?.[side] || 1;
    }

    /**
     * Determine if endpoints should be swapped to maintain consistent direction.
     * This ensures the platform always appears on the same side regardless of
     * which endpoints are selected as the "longest pair".
     *
     * Expected directions (based on getSideMultiplier assumptions):
     * - NS: North -> South (increasing y in screen coords, but depends on isometric)
     * - EW: East -> West (decreasing x in screen coords)
     * - NE_SW: NE -> SW (decreasing x+y in grid coords)
     * - NW_SE: NW -> SE (increasing x-y in grid coords)
     *
     * In isometric screen coords with our transform:
     * - Grid (x, y) -> Screen: screenX increases with grid x, screenY increases with grid x-y
     *
     * For consistent platform sides:
     * - NS: ensure dy > 0 (going "south" in screen space)
     * - EW: ensure dx < 0 (going "west" in screen space)
     * - NE_SW: ensure p1 is the NE point (higher x+y in screen)
     * - NW_SE: ensure p1 is the NW point (lower x in screen)
     */
    shouldSwapEndpoints(p1, p2, orientation) {
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;

        switch (orientation) {
            case 'NS':
                // For NS, direction should go "south" (positive screen y)
                // In isometric, moving south means dy should be positive
                return dy < 0;

            case 'EW':
                // For EW, direction should go "west" (negative screen x)
                return dx > 0;

            case 'NE_SW':
                // For NE_SW diagonal, direction should go from NE to SW
                // In screen coords, NE is up-right, SW is down-left
                // So we want the sum (x+y) to decrease (NE has higher x+y than SW)
                return (dx + dy) > 0;

            case 'NW_SE':
                // For NW_SE diagonal, direction should go from NW to SE
                // NW is up-left (lower x, higher y), SE is down-right (higher x, lower y)
                // So x should increase as we go from p1 to p2
                return dx < 0;

            default:
                return false;
        }
    }

    /**
     * Draw a quadrilateral platform shape.
     */
    drawPlatformQuad(x1, y1, x2, y2, x3, y3, x4, y4) {
        this.ctx.beginPath();
        this.ctx.moveTo(x1, y1);
        this.ctx.lineTo(x2, y2);
        this.ctx.lineTo(x3, y3);
        this.ctx.lineTo(x4, y4);
        this.ctx.closePath();
        this.ctx.fill();
        this.ctx.stroke();
    }

    drawTrain(train) {
        if (!train.position) return;

        // Convert grid to screen coordinates (isometric)
        const cx = this.gridToScreenX(train.position.x, train.position.y);
        const cy = this.gridToScreenY(train.position.x, train.position.y);

        this.ctx.save();
        this.ctx.translate(cx, cy);

        // Rotate based on direction
        let rotation = 0;
        if (train.direction === 'NORTH') rotation = -Math.PI / 2;
       
        else if (train.direction === 'SOUTH') rotation = Math.PI / 2;
        else if (train.direction === 'WEST') rotation = Math.PI;

        this.ctx.rotate(rotation);

        // Draw train body (scaled)
        const s = this.scale;
        this.ctx.fillStyle = '#8257e5';
        this.ctx.fillRect(-15 * s, -6 * s, 30 * s, 12 * s);

        // Draw cab
        this.ctx.fillStyle = '#9466ff';
        this.ctx.fillRect(5 * s, -6 * s, 10 * s, 12 * s);

        // Draw ID
        this.ctx.fillStyle = '#fff';
        this.ctx.font = `${8 * s}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.fillText(train.id, 0, 3 * s);

        this.ctx.restore();
    }

    // Helper to convert screen coords to grid coords (isometric)
    screenToGrid(screenX, screenY) {
        const x = this.screenToGridX(screenX, screenY);
        const y = this.screenToGridY(screenX, screenY);
        return { x, y };
    }

    // Draw preview path for drag-to-build
    drawPreviewPath(path) {
        if (!path || path.length === 0) return;

        this.ctx.save();

        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.6)';
        this.ctx.fillStyle = 'rgba(130, 87, 229, 0.3)';
        this.ctx.lineWidth = 3 * this.scale;
        this.ctx.setLineDash([5, 5]);

        const cellSize = this.gridSize * this.scale;

        // Draw path segments
        for (let i = 0; i < path.length; i++) {
            const pos = path[i];
            const cx = this.gridToScreenX(pos.x, pos.y);
            const cy = this.gridToScreenY(pos.x, pos.y);

            // Draw marker at position
            this.ctx.beginPath();
            this.ctx.arc(cx, cy, cellSize / 3, 0, Math.PI * 2);
            this.ctx.fill();

            // Draw connections to next cell
            if (i < path.length - 1) {
                const next = path[i + 1];
                const nx = this.gridToScreenX(next.x, next.y);
                const ny = this.gridToScreenY(next.x, next.y);

                this.ctx.beginPath();
                this.ctx.moveTo(cx, cy);
                this.ctx.lineTo(nx, ny);
                this.ctx.stroke();
            }
        }

        this.ctx.restore();
    }

    // Draw preview tracks with actual track geometry (semi-transparent light yellow)
    drawPreviewTracks(tracks) {
        if (!tracks || tracks.length === 0) {
            return;
        }

        const cellSize = this.gridSize * this.scale;
        const half = cellSize / 2;
        const previewColor = 'rgba(255, 200, 50, 0.8)'; // Light yellow preview color

        for (const track of tracks) {
            const trackBits = track.track_bits ?? 0;
            if (trackBits === 0) continue; // Skip empty tracks

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);

            this.ctx.save();
            this.ctx.translate(cx, cy);

            // Draw track segments using the same rendering logic
            const edgePoints = this.getEdgePoints(half);
            this.ctx.strokeStyle = previewColor;
            this.ctx.lineWidth = 5 * this.scale; // Slightly thicker for visibility
            this.ctx.lineCap = 'square';

            // Draw each active segment
            const segments = getActiveSegments(trackBits);
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

            // Draw crossing indicator for multi-segment crossings
            if (isCrossing(trackBits)) {
                this.ctx.fillStyle = 'rgba(255, 200, 50, 0.9)';
                this.ctx.beginPath();
                this.ctx.arc(0, 0, 4 * this.scale, 0, Math.PI * 2);
                this.ctx.fill();
            }

            this.ctx.restore();
        }
    }

    // Draw a selection highlight
    drawSelection(gridX, gridY) {
        this.ctx.save();

        const tileSize = this.gridSize * this.scale;
        const cx = this.gridToScreenX(gridX, gridY);
        const cy = this.gridToScreenY(gridX, gridY);
        const halfTile = tileSize / 2;

        this.ctx.strokeStyle = '#8257e5';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.setLineDash([4, 4]);

        // Draw diamond selection (1:1 square rotated 45 degrees)
        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy - halfTile);       // top
        this.ctx.lineTo(cx + halfTile, cy);       // right
        this.ctx.lineTo(cx, cy + halfTile);       // bottom
        this.ctx.lineTo(cx - halfTile, cy);       // left
        this.ctx.closePath();
        this.ctx.stroke();

        this.ctx.restore();
    }

    // ============= Debug Visualization Methods =============

    // Draw track_bits value in corner of tile
    drawTrackBitsIndicator(x, y, trackBits) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        this.ctx.save();
        this.ctx.font = `${10 * this.scale}px monospace`;
        this.ctx.fillStyle = '#888';
        this.ctx.textAlign = 'right';
        this.ctx.textBaseline = 'bottom';

        // Draw in bottom-right corner
        this.ctx.fillText(
            trackBits.toString(),
            screenX + cellSize * 0.45,
            screenY + cellSize * 0.45
        );

        this.ctx.restore();
    }

    // Draw individual bit visualization
    drawBitVisualization(x, y, trackBits) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        // Draw small indicators for each bit
        const bitPositions = {
            'NS': { x: 0.1, y: 0.1, bit: 1 },
            'EW': { x: 0.3, y: 0.1, bit: 2 },
            'NE': { x: 0.5, y: 0.1, bit: 4 },
            'NW': { x: 0.7, y: 0.1, bit: 8 },
            'SE': { x: 0.1, y: 0.3, bit: 16 },
            'SW': { x: 0.3, y: 0.3, bit: 32 }
        };

        this.ctx.save();
        for (const pos of Object.values(bitPositions)) {
            const isActive = (trackBits & pos.bit) !== 0;

            this.ctx.fillStyle = isActive ? '#00ff88' : '#333';
            this.ctx.beginPath();
            this.ctx.arc(
                screenX - cellSize * 0.5 + cellSize * pos.x,
                screenY - cellSize * 0.5 + cellSize * pos.y,
                3 * this.scale,
                0, Math.PI * 2
            );
            this.ctx.fill();
        }
        this.ctx.restore();
    }

    // Draw coordinate label
    drawCoordinateLabel(x, y) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        this.ctx.save();
        this.ctx.font = `${8 * this.scale}px monospace`;
        this.ctx.fillStyle = '#666';
        this.ctx.textAlign = 'left';
        this.ctx.textBaseline = 'top';
        this.ctx.fillText(
            `(${x},${y})`,
            screenX - cellSize * 0.45,
            screenY - cellSize * 0.45
        );
        this.ctx.restore();
    }

    // Draw dead-end edge markers (orange highlight at edge midpoint)
    // Markers are drawn orthogonal to the path direction
    // This is a permanent feature, always drawn for tracks with dead ends
    highlightDeadEnds(x, y, deadEnds, trackBits = 0) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const tileSize = this.gridSize * this.scale;
        const quarterTile = tileSize / 4;
        const markerLen = 6 * this.scale;

        // Track bit constants
        const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;

        // Port to path mappings (which paths use which ports)
        const portToPaths = {
            'N': [TRACK_NS, TRACK_NE, TRACK_NW],
            'S': [TRACK_NS, TRACK_SE, TRACK_SW],
            'E': [TRACK_EW, TRACK_NE, TRACK_SE],
            'W': [TRACK_EW, TRACK_NW, TRACK_SW]
        };

        // Perpendicular direction vectors for each path type (in screen coordinates)
        // These are unit vectors perpendicular to the path direction
        const pathPerpendicular = {
            [TRACK_NS]: { dx: 1, dy: 1 },    // NS runs ↗↙, perpendicular is ↘↖
            [TRACK_EW]: { dx: 1, dy: -1 },   // EW runs ↖↘, perpendicular is ↗↙
            [TRACK_NE]: { dx: 1, dy: 0 },    // NE is vertical on screen, perpendicular is horizontal
            [TRACK_NW]: { dx: 0, dy: 1 },    // NW is horizontal on screen, perpendicular is vertical
            [TRACK_SE]: { dx: 0, dy: 1 },    // SE is horizontal on screen, perpendicular is vertical
            [TRACK_SW]: { dx: 1, dy: 0 }     // SW is vertical on screen, perpendicular is horizontal
        };

        // Edge midpoint positions (relative to tile center)
        const edgePositions = {
            'N': { x: quarterTile, y: -quarterTile },
            'E': { x: quarterTile, y: quarterTile },
            'S': { x: -quarterTile, y: quarterTile },
            'W': { x: -quarterTile, y: -quarterTile }
        };

        this.ctx.save();
        this.ctx.strokeStyle = '#ff9900';
        this.ctx.lineWidth = 3 * this.scale;

        for (const edge of deadEnds) {
            const port = edge.length > 1 ? edge[0] : edge; // 'NORTH' -> 'N', 'N' -> 'N'
            const paths = portToPaths[port];
            if (!paths) continue;

            // Find which path uses this port (prioritize long paths NS/EW)
            let activePath = null;
            for (const pathBit of paths) {
                if (trackBits & pathBit) {
                    activePath = pathBit;
                    // Prefer NS/EW (long paths) over diagonals
                    if (pathBit === TRACK_NS || pathBit === TRACK_EW) break;
                }
            }

            // Get perpendicular direction (fallback to grid-aligned if no path found)
            let dx, dy;
            if (activePath && pathPerpendicular[activePath]) {
                dx = pathPerpendicular[activePath].dx;
                dy = pathPerpendicular[activePath].dy;
            } else {
                // Fallback: use grid-aligned diagonal (original behavior)
                dx = (port === 'N' || port === 'S') ? 1 : -1;
                dy = 1;
            }

            // Normalize for marker length
            const len = Math.sqrt(dx * dx + dy * dy);
            dx = (dx / len) * markerLen;
            dy = (dy / len) * markerLen;

            // Draw marker at edge midpoint
            const pos = edgePositions[port];
            const cx = screenX + pos.x;
            const cy = screenY + pos.y;

            this.ctx.beginPath();
            this.ctx.moveTo(cx - dx, cy - dy);
            this.ctx.lineTo(cx + dx, cy + dy);
            this.ctx.stroke();
        }
        this.ctx.restore();
    }

    // Draw comprehensive switch state overlay
    drawSwitchStateOverlay(track) {
        const screenX = this.gridToScreenX(track.x, track.y);
        const screenY = this.gridToScreenY(track.x, track.y);

        this.ctx.save();

        const stateText = track.switch_state === 0 ? 'MAIN' : 'DIV';
        const isDiagonalOnly = track.is_diagonal_only_switch;

        // State indicator
        this.ctx.font = `bold ${11 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';

        // Background
        const bgColor = track.switch_state === 0 ? '#1a5a2a' : '#5a4a1a';
        const textColor = track.switch_state === 0 ? '#04d361' : '#ffd93d';

        this.ctx.fillStyle = bgColor;
        this.ctx.globalAlpha = 0.9;
        this.ctx.beginPath();
        this.ctx.roundRect(screenX - 25 * this.scale, screenY - 18 * this.scale, 50 * this.scale, 16 * this.scale, 3);
        this.ctx.fill();
        this.ctx.globalAlpha = 1;

        // State text
        this.ctx.fillStyle = textColor;
        this.ctx.fillText(stateText, screenX, screenY - 10 * this.scale);

        // Special indicator for diagonal-only switches
        if (isDiagonalOnly) {
            this.ctx.font = `${8 * this.scale}px sans-serif`;
            this.ctx.fillStyle = '#ff9900';
            this.ctx.fillText('⟳clockwise', screenX, screenY + 6 * this.scale);
        }

        this.ctx.restore();
    }

    // Draw edge labels (N/E/S/W) at each edge midpoint for debugging
    drawEdgeLabels(x, y) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const tileSize = this.gridSize * this.scale;
        const quarterTile = tileSize / 4;

        this.ctx.save();
        this.ctx.font = `bold ${9 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillStyle = '#00ffff';

        // Edge midpoints (matching getEdgePoints)
        // N = top-right edge midpoint (Y+ neighbor, up-right on screen)
        this.ctx.fillText('N', screenX + quarterTile + 8, screenY - quarterTile - 8);
        // E = bottom-right edge midpoint (X+ neighbor, down-right on screen)
        this.ctx.fillText('E', screenX + quarterTile + 8, screenY + quarterTile + 8);
        // S = bottom-left edge midpoint (Y- neighbor, down-left on screen)
        this.ctx.fillText('S', screenX - quarterTile - 8, screenY + quarterTile + 8);
        // W = top-left edge midpoint (X- neighbor, up-left on screen)
        this.ctx.fillText('W', screenX - quarterTile - 8, screenY - quarterTile - 8);

        this.ctx.restore();
    }

    // Draw all debug overlays based on checkbox states
    drawDebugOverlays(tracks) {
        const showBits = document.getElementById('debug-show-bits')?.checked;
        const showCoords = document.getElementById('debug-show-coords')?.checked;
        const showSwitches = document.getElementById('debug-show-switches')?.checked;
        const showEdges = document.getElementById('debug-show-edges')?.checked;
        const showBlocks = document.getElementById('debug-show-blocks')?.checked;

        // Signal-to-block connections are now drawn separately before signals for better layering

        for (const track of tracks) {
            if (showBits && track.track_bits !== undefined) {
                this.drawBitVisualization(track.x, track.y, track.track_bits);
                this.drawTrackBitsIndicator(track.x, track.y, track.track_bits);
            }

            if (showCoords) {
                this.drawCoordinateLabel(track.x, track.y);
            }

            if (showSwitches && track.is_switch) {
                this.drawSwitchStateOverlay(track);
            }

            if (showEdges) {
                this.drawEdgeLabels(track.x, track.y);
            }
        }
    }

    // Generate unique color from block ID using golden angle distribution
    // Golden angle (137.508°) ensures maximum color separation between consecutive blocks
    getBlockColor(blockId) {
        if (!blockId) return null;

        // Check cache first
        if (this.blockColorCache[blockId] !== undefined) {
            const hue = this.blockColorCache[blockId];
            return `hsla(${hue}, 70%, 50%, 0.6)`;
        }

        // Assign new hue using golden angle distribution
        // Golden angle = 360° * (1 - 1/φ) ≈ 137.508°
        const goldenAngle = 137.508;
        const hue = (this.blockColorCounter * goldenAngle) % 360;

        // Cache the hue for this block
        this.blockColorCache[blockId] = hue;
        this.blockColorCounter++;

        return `hsla(${hue}, 70%, 50%, 0.6)`;
    }

    // Draw block ID colors as thicker lines along track paths
    drawBlockIdColors(tracks) {
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        for (const track of tracks) {
            if (!track.block_section_id) continue;

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);
            const color = this.getBlockColor(track.block_section_id);

            // Draw thicker colored segments along track paths
            this.ctx.save();
            this.ctx.translate(cx, cy);

            const trackBits = track.track_bits || 0;
            const segments = getActiveSegments(trackBits);
            const edgePoints = this.getEdgePoints(halfTile);

            for (const [e1, e2] of segments) {
                const start = edgePoints[e1];
                const end = edgePoints[e2];
                if (start && end) {
                    this.drawGridSegment(start, end, DEBUG_BLOCK_WIDTH * this.scale, e1, e2, color);
                }
            }

            this.ctx.restore();
        }

        // Block detail labels are now drawn separately after tracks
        // to ensure they appear on top of everything
    }

    /**
     * Draw small grey rectangles at track edges where the block changes.
     * This creates a visible gap between different blocks without needing debug labels.
     * Rectangles are ROTATED to align with track segments and SIZED based on segment type.
     * 
     * For long paths (NS/EW): 2px width × 4px height - stays completely within grid
     * For short paths (diagonals): 4px width × 4px height - extends slightly beyond grid
     * 
     * For each track, check each connected edge. If the neighbor tile exists and has
     * a different block_section_id, draw a rotated grey rectangle at that edge midpoint.
     */
    drawBlockBoundaryMarkers(tracks) {
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        // Build a lookup map for quick neighbor access
        const trackMap = {};
        for (const track of tracks) {
            trackMap[`${track.x},${track.y}`] = track;
        }

        // Direction deltas for finding neighbors
        const neighborDeltas = {
            'N': { dx: 0, dy: 1 },   // N edge connects to tile at (x, y+1)
            'E': { dx: 1, dy: 0 },   // E edge connects to tile at (x+1, y)
            'S': { dx: 0, dy: -1 },  // S edge connects to tile at (x, y-1)
            'W': { dx: -1, dy: 0 }   // W edge connects to tile at (x-1, y)
        };

        for (const track of tracks) {
            if (!track.block_section_id) continue;

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);
            const edgePoints = this.getEdgePoints(halfTile);

            // Get which edges this track connects to
            const trackBits = track.track_bits || 0;
            const connectedEdges = trackBitsToEdges(trackBits);

            // Get all track segments on this tile for rotation detection
            const segments = getActiveSegments(trackBits);

            for (const edge of connectedEdges) {
                const delta = neighborDeltas[edge];
                if (!delta) continue;

                const neighborX = track.x + delta.dx;
                const neighborY = track.y + delta.dy;
                const neighbor = trackMap[`${neighborX},${neighborY}`];

                // Draw boundary marker if:
                // 1. No neighbor exists (edge of network) - skip, not a block boundary
                // 2. Neighbor exists but has different block - draw marker
                // 3. Neighbor exists but has no block - draw marker
                if (!neighbor) continue;  // Edge of network, not a block boundary

                const neighborBlockId = neighbor.block_section_id;
                if (neighborBlockId === track.block_section_id) continue;  // Same block

                // Different blocks (or neighbor has no block) - draw grey marker
                const ep = edgePoints[edge];
                if (!ep) continue;

                // Find which segment(s) on this tile use this edge
                // We need to know the OTHER edge to determine path direction
                const opposites = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' };
                let segmentPartner = null;
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

                // Calculate path direction from boundary edge toward segment partner
                const epStart = edgePoints[edge];
                const epEnd = edgePoints[segmentPartner];
                const dx = epEnd.x - epStart.x;
                const dy = epEnd.y - epStart.y;
                const length = Math.sqrt(dx * dx + dy * dy);

                // Create a segment extending (TRACK_WIDTH / 2)px inward from boundary edge
                const markerDepth = (TRACK_WIDTH * this.scale) / 2;
                const segmentStart = epStart;
                const segmentEnd = {
                    x: epStart.x + (dx / length) * markerDepth,
                    y: epStart.y + (dy / length) * markerDepth
                };

                // Draw marker segment with track width
                const opposite_edge = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' }[edge];
                this.ctx.save();
                this.ctx.translate(cx, cy);
                this.drawGridSegment(
                    segmentStart,
                    segmentEnd,
                    TRACK_WIDTH * this.scale,
                    edge, 
                    opposite_edge,
                    '#666'
                );
                this.ctx.restore();
            }
        }
    }

    // Draw block detail labels - multiline text box on top of tracks
    drawBlockDetailLabels(tracks) {
        // Group tracks by block_section_id and find center tile
        const blockTracks = {};
        for (const track of tracks) {
            if (!track.block_section_id) continue;
            if (!blockTracks[track.block_section_id]) {
                blockTracks[track.block_section_id] = [];
            }
            blockTracks[track.block_section_id].push(track);
        }

        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        // First pass: find which block is being hovered
        let hoveredBlockId = null;
        let hoveredBlockData = null;

        for (const [blockId, blockTrackList] of Object.entries(blockTracks)) {
            if (blockTrackList.length === 0) continue;

            const centerIndex = Math.floor(blockTrackList.length / 2);
            const centerTrack = blockTrackList[centerIndex];
            const cx = this.gridToScreenX(centerTrack.x, centerTrack.y);
            const cy = this.gridToScreenY(centerTrack.x, centerTrack.y);

            const offsetY = tileSize * 0.35;
            const offsetX = tileSize * 0.3;
            const neOffsetX = 10;
            const neOffsetY = -10;

            // Estimate label bounds (approximate)
            const labelX = cx + offsetX + neOffsetX;
            const labelY = cy + offsetY + neOffsetY;
            const labelWidth = 100 * this.scale; // Approximate width
            const labelHeight = 40 * this.scale; // Approximate height

            // Check if mouse is over this label
            if (this.mouseX >= labelX - 5 && this.mouseX <= labelX + labelWidth + 5 &&
                this.mouseY >= labelY - 5 && this.mouseY <= labelY + labelHeight + 5) {
                hoveredBlockId = blockId;
                hoveredBlockData = {
                    blockTrackList,
                    centerTrack,
                    cx, cy
                };
                break;
            }
        }

        // Second pass: draw non-hovered blocks (70% alpha, single line only)
        for (const [blockId, blockTrackList] of Object.entries(blockTracks)) {
            if (blockId === hoveredBlockId) continue; // Skip hovered block
            if (blockTrackList.length === 0) continue;

            const centerIndex = Math.floor(blockTrackList.length / 2);
            const centerTrack = blockTrackList[centerIndex];
            const cx = this.gridToScreenX(centerTrack.x, centerTrack.y);
            const cy = this.gridToScreenY(centerTrack.x, centerTrack.y);

            const blockInfo = this.currentBlocks?.[blockId] || {};
            const blockName = blockInfo.name || '';
            const shortId = blockId.length > 10 ? blockId.slice(-6) : blockId;
            const displayName = blockName || shortId;

            const offsetY = tileSize * 0.35;
            const offsetX = tileSize * 0.3;
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

            // Draw background box with 70% alpha
            this.ctx.fillStyle = 'rgba(13, 16, 32, 0.7)';
            this.ctx.strokeStyle = 'rgba(100, 100, 150, 0.4)';
            this.ctx.lineWidth = 1;
            this.ctx.beginPath();
            this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
            this.ctx.fill();
            this.ctx.stroke();

            // Line 1: Block name only
            this.ctx.fillStyle = 'rgba(221, 221, 221, 0.7)';
            this.ctx.fillText(line1, cx + offsetX + neOffsetX, cy + offsetY + neOffsetY);

            this.ctx.restore();
        }

        // Third pass: if there's a hovered block, highlight tracks and draw full info
        if (hoveredBlockId && hoveredBlockData) {
            const { blockTrackList, centerTrack, cx, cy } = hoveredBlockData;
            const blockInfo = this.currentBlocks?.[hoveredBlockId] || {};

            // Draw highlighted tracks on top
            this.ctx.save();
            const blockColor = this.getBlockColor(hoveredBlockId);
            const edgePoints = this.getEdgePoints(halfTile);

            for (const track of blockTrackList) {
                const trackCx = this.gridToScreenX(track.x, track.y);
                const trackCy = this.gridToScreenY(track.x, track.y);

                this.ctx.save();
                this.ctx.translate(trackCx, trackCy);

                const trackBits = track.track_bits || 0;
                const segments = getActiveSegments(trackBits);

                this.ctx.strokeStyle = blockColor.replace('0.6', '0.9'); // Increase opacity
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

            // Draw full label with 100% alpha
            const blockName = blockInfo.name || '';
            const blockState = (blockInfo.state || 'FREE').toUpperCase();
            const stateColors = {
                'FREE': '#04d361',
                'RESERVED': '#ffd93d',
                'OCCUPIED': '#e83f5b',
                'CAUTION': '#ff9500'
            };
            const stateColor = stateColors[blockState] || '#888';

            const shortId = hoveredBlockId.length > 10 ? hoveredBlockId.slice(-6) : hoveredBlockId;
            const displayName = blockName || shortId;

            const offsetY = tileSize * 0.35;
            const offsetX = tileSize * 0.3;
            const neOffsetX = 10;
            const neOffsetY = -10;

            this.ctx.save();

            const fontSize = Math.max(5, 8 * this.scale);
            this.ctx.font = `${fontSize}px sans-serif`;
            this.ctx.textAlign = 'left';
            this.ctx.textBaseline = 'top';
            const lineHeight = fontSize + 2;
            const trackCount = blockTrackList.length;

            // Build all lines
            const line1 = displayName;
            const line2 = `${blockState} ${trackCount} tiles`;
            const lengthStr = blockInfo.length_cm ? `${blockInfo.length_cm}cm` : '-';
            const radiusStr = blockInfo.min_radius_cm ? `R${blockInfo.min_radius_cm}` : '-';
            const speedStr = blockInfo.max_speed_kmh ? `${blockInfo.max_speed_kmh}km/h` : '-';
            const line3 = `${lengthStr} ${radiusStr} ${speedStr}`;

            const maxWidth = Math.max(
                this.ctx.measureText(line1).width,
                this.ctx.measureText(line2).width,
                this.ctx.measureText(line3).width
            );

            const padding = 1;
            const boxX = cx + offsetX + neOffsetX - padding;
            const boxY = cy + offsetY + neOffsetY - padding;
            const boxWidth = maxWidth + padding * 2;
            const boxHeight = lineHeight * 3 + padding * 2;

            // Draw background box with 100% alpha
            this.ctx.fillStyle = 'rgba(13, 16, 32, 1.0)';
            this.ctx.strokeStyle = 'rgba(100, 100, 150, 1.0)';
            this.ctx.lineWidth = 1;
            this.ctx.beginPath();
            this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
            this.ctx.fill();
            this.ctx.stroke();

            let yPos = cy + offsetY + neOffsetY;

            // Line 1: Block name (white)
            this.ctx.fillStyle = '#ddd';
            this.ctx.fillText(line1, cx + offsetX + neOffsetX, yPos);
            yPos += lineHeight;

            // Line 2: State and track count (colored by state)
            this.ctx.fillStyle = stateColor;
            this.ctx.fillText(line2, cx + offsetX + neOffsetX, yPos);
            yPos += lineHeight;

            // Line 3: Length, radius, speed (gray)
            this.ctx.fillStyle = '#999';
            this.ctx.fillText(line3, cx + offsetX + neOffsetX, yPos);

            this.ctx.restore();
        }
    }

    // Draw dotted lines from signals to blocks they monitor
    drawSignalBlockConnections(tracks) {
        // Collect all track positions for each block
        const blockTracks = {};
        for (const track of tracks) {
            if (track.block_section_id) {
                if (!blockTracks[track.block_section_id]) {
                    blockTracks[track.block_section_id] = [];
                }
                blockTracks[track.block_section_id].push({
                    x: track.x,
                    y: track.y,
                    track_bits: track.track_bits,
                    screenX: this.gridToScreenX(track.x, track.y),
                    screenY: this.gridToScreenY(track.x, track.y)
                });
            }
        }

        const half = (this.gridSize * this.scale) / 2;

        // Draw dotted lines from signals to their monitored blocks
        for (const track of tracks) {
            // Iterate over all signals on this track
            if (!track.signals || track.signals.length === 0) continue;

            for (const signal of track.signals) {
                const monitoredBlockId = signal.monitored_block_id;
                if (!monitoredBlockId || !blockTracks[monitoredBlockId]) continue;

                // Calculate signal circle center position
                // Signal position is offset based on direction (right-hand side rule)
                const tileX = this.gridToScreenX(track.x, track.y);
                const tileY = this.gridToScreenY(track.x, track.y);
                // detection_direction = where trains travel TO (required)
                if (!signal.detection_direction) {
                    console.error(`[Renderer] Signal ${signal.signal_id} missing detection_direction in block line drawing!`, signal);
                    continue;  // Skip this signal
                }
                const detectionDir = signal.detection_direction;
                const travelEdge = detectionDir.charAt(0).toUpperCase();
                const trackBits = track.track_bits || 0;
                const isLongPath = (trackBits === 1 || trackBits === 2);

                // Calculate signal position within tile (matching drawSignal logic)
                let signalOffsetX = 0, signalOffsetY = 0;
                if (isLongPath) {
                    const edgePoints = this.getEdgePoints(half);
                    const rightSide = { 'N': 'E', 'S': 'W', 'E': 'S', 'W': 'N' }[travelEdge] || 'E';
                    const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
                    signalOffsetX = sidePoint.x * 0.5;
                    signalOffsetY = sidePoint.y * 0.5;
                }
                const signalX = tileX + signalOffsetX;
                const signalY = tileY + signalOffsetY;

                // Find the nearest track in the monitored block
                const blockTrackList = blockTracks[monitoredBlockId];
                let nearestDist = Infinity;
                let nearestEdgeX = null, nearestEdgeY = null;

                for (const bt of blockTrackList) {
                    // Get the actual edge midpoints for this tile
                    const edgePointsRelative = this.getEdgePoints(half);

                    // Find which edges this track actually uses based on track_bits
                    const usedEdges = trackBitsToEdges(bt.track_bits || 0);

                    // Find the closest edge point where the track path actually ends
                    for (const edgeName of usedEdges) {
                        const ep = edgePointsRelative[edgeName];
                        if (!ep) continue;

                        // Convert relative position to absolute screen coordinates
                        const absX = bt.screenX + ep.x;
                        const absY = bt.screenY + ep.y;

                        const dx = absX - signalX;
                        const dy = absY - signalY;
                        const dist = dx * dx + dy * dy;
                        if (dist < nearestDist) {
                            nearestDist = dist;
                            nearestEdgeX = absX;
                            nearestEdgeY = absY;
                        }
                    }
                }

                // Draw thin dotted line from signal to nearest block track edge
                // Only draw if we found a valid edge point
                if (nearestEdgeX !== null && nearestEdgeY !== null) {
                    this.ctx.save();
                    this.ctx.strokeStyle = 'rgba(180, 100, 255, 0.6)';  // Purple/magenta
                    this.ctx.lineWidth = 2 * this.scale;
                    this.ctx.setLineDash([4 * this.scale, 4 * this.scale]);
                    this.ctx.beginPath();
                    this.ctx.moveTo(signalX, signalY);
                    this.ctx.lineTo(nearestEdgeX, nearestEdgeY);
                    this.ctx.stroke();
                    this.ctx.setLineDash([]);
                    this.ctx.restore();
                }
            }
        }
    }

    // Draw hover preview for signal/station tools
    // Signal preview now matches actual signal rendering position (drawSignal logic)
    // signalDirection is now the detection_direction (where trains travel TO)
    drawHoverPreview(gridPos, toolType, isValid, signalDirection = null, trackBits = null) {
        const cx = this.gridToScreenX(gridPos.x, gridPos.y);
        const cy = this.gridToScreenY(gridPos.x, gridPos.y);
        const half = (this.gridSize * this.scale) / 2;

        this.ctx.save();

        // Draw diamond highlight
        this.ctx.strokeStyle = isValid ? 'rgba(4, 211, 97, 0.8)' : 'rgba(232, 63, 91, 0.5)';
        this.ctx.fillStyle = isValid ? 'rgba(4, 211, 97, 0.2)' : 'rgba(232, 63, 91, 0.1)';
        this.ctx.lineWidth = 2 * this.scale;

        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy - half);
        this.ctx.lineTo(cx + half, cy);
        this.ctx.lineTo(cx, cy + half);
        this.ctx.lineTo(cx - half, cy);
        this.ctx.closePath();
        this.ctx.fill();
        this.ctx.stroke();

        // Draw tool icon preview
        if (isValid) {
            if (toolType === 'signal' && signalDirection) {
                // Draw signal preview matching actual signal rendering (drawSignal logic)
                // signalDirection = detection_direction (where trains travel TO)
                this.ctx.translate(cx, cy);

                const detectionDir = signalDirection.charAt(0).toUpperCase();
                const travelEdge = detectionDir;  // detection_direction IS the travel direction
                const edgePoints = this.getEdgePoints(half);
                const isLongPath = (trackBits === 1 || trackBits === 2);  // NS or EW straight tracks
                const isDiagonal = (trackBits === 4 || trackBits === 8 || trackBits === 16 || trackBits === 32);

                // Calculate signal position based on path type (same as drawSignal)
                let sx, sy;
                if (isLongPath) {
                    // Right-hand side rule: position on right side relative to train's TRAVEL direction
                    const rightSide = {
                        'N': 'E', 'S': 'W', 'E': 'S', 'W': 'N'
                    }[travelEdge] || 'E';
                    const sidePoint = edgePoints[rightSide] || { x: 0, y: 0 };
                    sx = sidePoint.x * 0.5;  // Quarter point toward that side
                    sy = sidePoint.y * 0.5;
                } else {
                    // Center for short paths (diagonals: NE, NW, SE, SW)
                    sx = 0;
                    sy = 0;
                }

                // Calculate direction for stick angle (same logic as drawSingleSignal)
                let dirX, dirY;
                if (isDiagonal) {
                    // For diagonal tracks, stick points ALONG the diagonal path (45 degree angle in isometric)
                    // Base diagonal vectors represent direction from edge2 toward edge1 of the track
                    // See drawSingleSignal for detailed vector calculation documentation
                    let diagonalX, diagonalY;
                    switch (trackBits) {
                        case 4: diagonalX = 0; diagonalY = -1; break;  // NE: vertical up (E→N)
                        case 8: diagonalX = 1; diagonalY = 0; break;  // NW: horizontal right (W→N)
                        case 16: diagonalX = -1; diagonalY = 0; break;  // SE: horizontal left (E→S)
                        case 32: diagonalX = 0; diagonalY = 1; break;  // SW: vertical down (W→S)
                        default: diagonalX = 1; diagonalY = 0; break;
                    }

                    // Determine the two edges of this diagonal
                    let edge1, edge2;
                    switch (trackBits) {
                        case 4: edge1 = 'N'; edge2 = 'E'; break;
                        case 8: edge1 = 'N'; edge2 = 'W'; break;
                        case 16: edge1 = 'S'; edge2 = 'E'; break;
                        case 32: edge1 = 'S'; edge2 = 'W'; break;
                        default: edge1 = 'N'; edge2 = 'E'; break;
                    }

                    // Use detection direction (not approach) to determine which way to point
                    // If train exits via edge1, it approaches from edge2 → flip base to point toward edge2
                    if (detectionDir === edge1) {
                        diagonalX = -diagonalX;
                        diagonalY = -diagonalY;
                    }

                    // Normalize
                    const len = Math.sqrt(diagonalX * diagonalX + diagonalY * diagonalY);
                    dirX = diagonalX / len;
                    dirY = diagonalY / len;
                } else {
                    // For straight tracks, stick points in approach direction (where train comes FROM)
                    // Compute approach direction (opposite of detection direction)
                    const approachEdge = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' }[detectionDir] || 'W';
                    const dirPoint = edgePoints[approachEdge] || { x: 1, y: 0 };
                    const len = Math.sqrt(dirPoint.x * dirPoint.x + dirPoint.y * dirPoint.y);
                    dirX = dirPoint.x / len;
                    dirY = dirPoint.y / len;
                }

                // Draw grey outer circle (preview style - semi-transparent)
                this.ctx.strokeStyle = 'rgba(153, 153, 153, 0.8)';
                this.ctx.lineWidth = 1.5 * this.scale;
                this.ctx.beginPath();
                this.ctx.arc(sx, sy, 4 * this.scale, 0, Math.PI * 2);
                this.ctx.stroke();

                // Draw stick parallel to path direction
                const stickLength = half * 0.4;
                const stickEndX = sx + dirX * stickLength;
                const stickEndY = sy + dirY * stickLength;

                this.ctx.strokeStyle = 'rgba(153, 153, 153, 0.8)';
                this.ctx.lineWidth = 2 * this.scale;
                this.ctx.beginPath();
                this.ctx.moveTo(sx, sy);
                this.ctx.lineTo(stickEndX, stickEndY);
                this.ctx.stroke();

                // Draw colored inner circle (green preview)
                const innerRadius = 3 * this.scale;
                this.ctx.fillStyle = 'rgba(4, 211, 97, 0.7)';  // Green semi-transparent
                this.ctx.beginPath();
                this.ctx.arc(sx, sy, innerRadius, 0, Math.PI * 2);
                this.ctx.fill();

                // Draw orthogonal stroke at end of stick
                const orthoX = -dirY;
                const orthoY = dirX;
                const orthoLen = half * 0.15;

                this.ctx.beginPath();
                this.ctx.moveTo(stickEndX - orthoX * orthoLen, stickEndY - orthoY * orthoLen);
                this.ctx.lineTo(stickEndX + orthoX * orthoLen, stickEndY + orthoY * orthoLen);
                this.ctx.stroke();

                // Draw direction label
                this.ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
                this.ctx.font = `bold ${8 * this.scale}px sans-serif`;
                this.ctx.textAlign = 'center';
                this.ctx.textBaseline = 'middle';
                // Position label opposite to stick direction
                const labelX = sx - dirX * half * 0.3;
                const labelY = sy - dirY * half * 0.3;
                this.ctx.fillText(signalDirection.charAt(0), labelX, labelY);

            } else {
                // Draw icon
                this.ctx.font = `${16 * this.scale}px sans-serif`;
                this.ctx.textAlign = 'center';
                this.ctx.textBaseline = 'middle';
                this.ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
                const icon = toolType === 'signal' ? '🚦' : '🚉';
                this.ctx.fillText(icon, cx, cy);
            }
        }

        this.ctx.restore();
    }

    // Draw eraser preview path in red
    drawEraserPreviewPath(path) {
        if (!path || path.length === 0) return;

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(232, 63, 91, 0.8)';  // Red
        this.ctx.fillStyle = 'rgba(232, 63, 91, 0.3)';
        this.ctx.lineWidth = 4 * this.scale;
        this.ctx.setLineDash([6, 4]);

        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        // Draw X marks on each tile to be erased
        for (const pos of path) {
            const cx = this.gridToScreenX(pos.x, pos.y);
            const cy = this.gridToScreenY(pos.x, pos.y);

            // Fill diamond
            this.ctx.beginPath();
            this.ctx.moveTo(cx, cy - halfTile);
            this.ctx.lineTo(cx + halfTile, cy);
            this.ctx.lineTo(cx, cy + halfTile);
            this.ctx.lineTo(cx - halfTile, cy);
            this.ctx.closePath();
            this.ctx.fill();

            // Draw X
            const xSize = halfTile * 0.4;
            this.ctx.beginPath();
            this.ctx.moveTo(cx - xSize, cy - xSize);
            this.ctx.lineTo(cx + xSize, cy + xSize);
            this.ctx.moveTo(cx + xSize, cy - xSize);
            this.ctx.lineTo(cx - xSize, cy + xSize);
            this.ctx.stroke();
        }

        // Connect path with line
        this.ctx.setLineDash([]);
        this.ctx.strokeStyle = 'rgba(232, 63, 91, 0.6)';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        for (let i = 0; i < path.length; i++) {
            const cx = this.gridToScreenX(path[i].x, path[i].y);
            const cy = this.gridToScreenY(path[i].x, path[i].y);
            if (i === 0) this.ctx.moveTo(cx, cy);
            else this.ctx.lineTo(cx, cy);
        }
        this.ctx.stroke();

        this.ctx.restore();
    }

    // Draw block assignment preview path in purple
    drawBlockPreviewPath(path) {
        if (!path || path.length === 0) return;

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.8)';  // Purple
        this.ctx.fillStyle = 'rgba(130, 87, 229, 0.3)';
        this.ctx.lineWidth = 4 * this.scale;
        this.ctx.setLineDash([6, 4]);

        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        // Draw block indicator on each tile
        for (const pos of path) {
            const cx = this.gridToScreenX(pos.x, pos.y);
            const cy = this.gridToScreenY(pos.x, pos.y);

            // Fill diamond
            this.ctx.beginPath();
            this.ctx.moveTo(cx, cy - halfTile);
            this.ctx.lineTo(cx + halfTile, cy);
            this.ctx.lineTo(cx, cy + halfTile);
            this.ctx.lineTo(cx - halfTile, cy);
            this.ctx.closePath();
            this.ctx.fill();

            // Draw block symbol (square)
            const boxSize = halfTile * 0.3;
            this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
            this.ctx.lineWidth = 2 * this.scale;
            this.ctx.setLineDash([]);
            this.ctx.strokeRect(cx - boxSize, cy - boxSize, boxSize * 2, boxSize * 2);
            this.ctx.setLineDash([6, 4]);
            this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.8)';
        }

        // Connect path with line
        this.ctx.setLineDash([]);
        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.6)';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        for (let i = 0; i < path.length; i++) {
            const cx = this.gridToScreenX(path[i].x, path[i].y);
            const cy = this.gridToScreenY(path[i].x, path[i].y);
            if (i === 0) this.ctx.moveTo(cx, cy);
            else this.ctx.lineTo(cx, cy);
        }
        this.ctx.stroke();

        this.ctx.restore();
    }

    // === Route Overlay Methods ===

    /**
     * Set the reachable highlights for route creation mode
     * @param {Set<string>} positionsSet - Set of "x,y" strings
     */
    setReachableHighlights(positionsSet) {
        this.reachableHighlights = positionsSet || new Set();
    }

    /**
     * Set active routes for visualization
     * @param {Array} routes - Route objects from API
     */
    setRoutes(routes) {
        this.activeRoutes = routes || [];
    }

    /**
     * Highlight a specific route
     * @param {Object} route - Route object to highlight
     */
    highlightRoute(route) {
        this.highlightedRouteId = route?.route_id || null;
        if (this.currentNetwork) {
            this.drawNetwork(this.currentNetwork);
        }
    }

    /**
     * Clear route highlight
     */
    clearRouteHighlight() {
        this.highlightedRouteId = null;
        if (this.currentNetwork) {
            this.drawNetwork(this.currentNetwork);
        }
    }

    /**
     * Clear route preview (for route tool)
     */
    clearRoutePreview() {
        this.reachableHighlights = new Set();
    }

    /**
     * Flash conflict tiles for a duration
     * @param {Array} tiles - Array of [x, y] tuples
     * @param {number} durationMs - Flash duration in milliseconds
     */
    flashConflictTiles(tiles, durationMs = 2000) {
        this.conflictFlashTiles = tiles || [];
        this.conflictFlashEnd = Date.now() + durationMs;
        
        // Start animation loop if we have tiles to flash
        if (this.conflictFlashTiles.length > 0) {
            this._animateConflictFlash();
        }
    }

    /**
     * Animate conflict flash effect
     */
    _animateConflictFlash() {
        if (Date.now() >= this.conflictFlashEnd) {
            this.conflictFlashTiles = [];
            if (this.currentNetwork) {
                this.drawNetwork(this.currentNetwork);
            }
            return;
        }

        // Redraw with flash
        if (this.currentNetwork) {
            this.drawNetwork(this.currentNetwork);
        }

        // Continue animation
        requestAnimationFrame(() => this._animateConflictFlash());
    }

    /**
     * Draw all route overlays
     */
    drawRouteOverlays() {
        this.ctx.save();

        // 1. Draw reachable endpoint highlights (for route creation)
        if (this.reachableHighlights.size > 0) {
            this.drawReachableHighlights();
        }

        // 2. Draw active routes
        if (this.activeRoutes.length > 0) {
            for (const route of this.activeRoutes) {
                const isHighlighted = route.route_id === this.highlightedRouteId;
                this.drawRoutePath(route, isHighlighted);
            }
        }

        // 3. Draw highlighted route (if from a route not in activeRoutes, e.g., viewing details)
        if (this.highlightedRouteId && !this.activeRoutes.some(r => r.route_id === this.highlightedRouteId)) {
            // The route might be passed to highlightRoute directly, find it in network data
            // For now, just rely on activeRoutes having the data
        }

        // 4. Draw conflict flash
        if (Date.now() < this.conflictFlashEnd && this.conflictFlashTiles.length > 0) {
            this.drawConflictFlash();
        }

        this.ctx.restore();
    }

    /**
     * Draw reachable endpoint highlights (orange glow for route tool)
     */
    drawReachableHighlights() {
        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        this.ctx.fillStyle = 'rgba(255, 157, 0, 0.25)';  // Orange, 25% opacity

        for (const key of this.reachableHighlights) {
            const [x, y] = key.split(',').map(Number);
            const cx = this.gridToScreenX(x, y);
            const cy = this.gridToScreenY(x, y);

            // Draw diamond shape
            this.ctx.beginPath();
            this.ctx.moveTo(cx, cy - halfTile);
            this.ctx.lineTo(cx + halfTile, cy);
            this.ctx.lineTo(cx, cy + halfTile);
            this.ctx.lineTo(cx - halfTile, cy);
            this.ctx.closePath();
            this.ctx.fill();
        }

        // Draw border on reachable tiles
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

    /**
     * Draw a route path overlay
     * @param {Object} route - Route object
     * @param {boolean} emphasized - Whether to draw with emphasis
     */
    drawRoutePath(route, emphasized = false) {
        if (!route.path_tiles || route.path_tiles.length === 0) return;

        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        // Get color based on status
        const statusColors = {
            'pending': 'rgba(136, 136, 136, 0.3)',
            'setting': 'rgba(255, 217, 61, 0.3)',
            'set': 'rgba(4, 211, 97, 0.3)',
            'active': 'rgba(0, 255, 136, 0.4)',
            'clearing': 'rgba(255, 149, 0, 0.3)',
            'completed': 'rgba(102, 102, 102, 0.2)',
            'cancelled': 'rgba(232, 63, 91, 0.2)'
        };

        const fillOpacity = emphasized ? 0.4 : 0.2;
        const baseColor = statusColors[route.status] || 'rgba(100, 100, 100, 0.2)';
        
        // Adjust opacity for emphasis
        let fillColor = baseColor;
        if (emphasized) {
            fillColor = baseColor.replace(/[\d.]+\)$/, `${fillOpacity})`);
        }

        // Draw path tiles
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

        // Draw border if emphasized
        if (emphasized) {
            const borderColors = {
                'pending': '#888',
                'setting': '#ffd93d',
                'set': '#04d361',
                'active': '#00ff88',
                'clearing': '#ff9500',
                'completed': '#666',
                'cancelled': '#e83f5b'
            };

            this.ctx.strokeStyle = borderColors[route.status] || '#888';
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

        // Draw direction triangles on route tiles
        if (route.tile_directions) {
            this.drawRouteDirectionArrows(route, halfTile);
        }

        // Draw locked switch indicators (small lock icon)
        if (route.switch_positions && emphasized) {
            this.ctx.fillStyle = '#8B4513';  // Brown
            for (const key of Object.keys(route.switch_positions)) {
                const [x, y] = key.split(',').map(Number);
                const cx = this.gridToScreenX(x, y);
                const cy = this.gridToScreenY(x, y);

                // Draw small lock symbol
                const lockSize = 6 * this.scale;
                this.ctx.beginPath();
                this.ctx.arc(cx, cy - lockSize, lockSize * 0.6, 0, Math.PI * 2);
                this.ctx.fill();
                this.ctx.fillRect(cx - lockSize * 0.5, cy - lockSize * 0.5, lockSize, lockSize);
            }
        }
    }

    /**
     * Draw small direction triangles on route path tiles
     * @param {Object} route - Route object with tile_directions
     * @param {number} halfTile - Half tile size in pixels
     */
    drawRouteDirectionArrows(route, halfTile) {
        if (!route.tile_directions || !route.path_tiles) return;

        // Get color based on status (use lighter version for arrows)
        const arrowColors = {
            'pending': 'rgba(136, 136, 136, 0.8)',
            'setting': 'rgba(255, 217, 61, 0.9)',
            'set': 'rgba(4, 211, 97, 0.9)',
            'active': 'rgba(0, 255, 136, 0.9)',
            'clearing': 'rgba(255, 149, 0, 0.9)',
            'completed': 'rgba(102, 102, 102, 0.6)',
            'cancelled': 'rgba(232, 63, 91, 0.6)'
        };

        const arrowColor = arrowColors[route.status] || 'rgba(100, 100, 100, 0.8)';
        const arrowSize = halfTile * 0.3;  // Small triangle

        this.ctx.fillStyle = arrowColor;

        for (const tile of route.path_tiles) {
            const [x, y] = tile;
            const key = `${x},${y}`;
            const direction = route.tile_directions[key];
            
            if (!direction) continue;

            const cx = this.gridToScreenX(x, y);
            const cy = this.gridToScreenY(x, y);

            // Draw triangle pointing in the direction of travel
            // In isometric view:
            // N (Y+) -> up-left in screen space
            // S (Y-) -> down-right in screen space
            // E (X+) -> up-right in screen space
            // W (X-) -> down-left in screen space
            this.ctx.beginPath();
            
            switch (direction) {
                case 'N':  // Up-left in isometric
                    this.ctx.moveTo(cx - arrowSize * 0.8, cy - arrowSize * 0.4);
                    this.ctx.lineTo(cx + arrowSize * 0.4, cy + arrowSize * 0.2);
                    this.ctx.lineTo(cx + arrowSize * 0.2, cy - arrowSize * 0.6);
                    break;
                case 'S':  // Down-right in isometric
                    this.ctx.moveTo(cx + arrowSize * 0.8, cy + arrowSize * 0.4);
                    this.ctx.lineTo(cx - arrowSize * 0.4, cy - arrowSize * 0.2);
                    this.ctx.lineTo(cx - arrowSize * 0.2, cy + arrowSize * 0.6);
                    break;
                case 'E':  // Up-right in isometric
                    this.ctx.moveTo(cx + arrowSize * 0.8, cy - arrowSize * 0.4);
                    this.ctx.lineTo(cx - arrowSize * 0.4, cy + arrowSize * 0.2);
                    this.ctx.lineTo(cx - arrowSize * 0.2, cy - arrowSize * 0.6);
                    break;
                case 'W':  // Down-left in isometric
                    this.ctx.moveTo(cx - arrowSize * 0.8, cy + arrowSize * 0.4);
                    this.ctx.lineTo(cx + arrowSize * 0.4, cy - arrowSize * 0.2);
                    this.ctx.lineTo(cx + arrowSize * 0.2, cy + arrowSize * 0.6);
                    break;
            }
            
            this.ctx.closePath();
            this.ctx.fill();
        }
    }

    /**
     * Draw conflict flash overlay (red pulsing)
     */
    drawConflictFlash() {
        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        // Pulsing effect: toggle every 200ms
        const flash = Math.floor(Date.now() / 200) % 2 === 0;
        if (!flash) return;

        this.ctx.fillStyle = 'rgba(232, 63, 91, 0.5)';  // Red, 50% opacity

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

        // Draw X mark on conflict tiles
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
