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

class Renderer {
    constructor(canvasId) {
        console.log('[Renderer] Initializing with canvas ID:', canvasId);
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) {
            console.error('[Renderer] Canvas element not found!');
            return;
        }
        console.log('[Renderer] Canvas found:', this.canvas);
        this.ctx = this.canvas.getContext('2d');
        this.gridSize = 40;

        // Pan offset (user dragging)
        this.panX = 0;
        this.panY = 0;
        this.scale = 1.0;

        // Center of canvas (where grid 0,0 is displayed)
        this.centerX = 0;
        this.centerY = 0;

        // Block section colors
        this.blockColors = {
            'FREE': 'rgba(100, 100, 100, 0.2)',
            'RESERVED': 'rgba(255, 205, 30, 0.3)',
            'OCCUPIED': 'rgba(232, 63, 91, 0.3)',
            'LOCKED': 'rgba(66, 133, 244, 0.3)'
        };

        this.resize();
        window.addEventListener('resize', () => this.resize());
        console.log('[Renderer] Initialization complete');
    }

    resize() {
        const parent = this.canvas.parentElement;
        console.log('[Renderer] Parent element:', parent);

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

        console.log('[Renderer] Resized canvas to', width, 'x', height, 'center at', this.centerX, this.centerY);
        this.draw();
    }

    draw() {
        // Basic draw - just clear and draw grid if no network loaded
        console.log('[Renderer] draw() called, canvas size:', this.canvas.width, 'x', this.canvas.height);
        this.clear();
        this.drawGrid();
        console.log('[Renderer] Basic draw completed');
    }

    clear() {
        this.ctx.fillStyle = '#0a0a0c';
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
        console.log('[Renderer] Cleared canvas');
    }

    drawGrid() {
        console.log('[Renderer] Drawing isometric diamond grid...');
        this.ctx.save();

        // 1:1 square diamond tile (rotated 45 degrees)
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;
        const quarter = tileSize / 4;

        // Calculate visible grid range (approximate)
        const viewWidth = this.canvas.width;
        const viewHeight = this.canvas.height;

        // Rough estimate of visible tiles
        const range = Math.ceil(Math.max(viewWidth, viewHeight) / tileSize) + 4;

        // Draw diamond grid lines
        this.ctx.strokeStyle = '#2a2a34';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();

        // Draw diagonal lines forming diamonds
        for (let gx = -range; gx <= range; gx++) {
            for (let gy = -range; gy <= range; gy++) {
                // Get screen position of this grid cell's center
                const centerX = this.gridToScreenX(gx, gy);
                const centerY = this.gridToScreenY(gx, gy);

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

        this.ctx.strokeStyle = '#4a4a5a';
        this.ctx.lineWidth = 2;

        // X-axis (Y=0 line) - goes from top-left to bottom-right on screen
        this.ctx.beginPath();
        this.ctx.moveTo(gridPointX(-range, 0), gridPointY(-range, 0));
        this.ctx.lineTo(gridPointX(range, 0), gridPointY(range, 0));
        this.ctx.stroke();

        // Y-axis (X=0 line) - goes from top-right to bottom-left on screen
        this.ctx.beginPath();
        this.ctx.moveTo(gridPointX(0, -range), gridPointY(0, -range));
        this.ctx.lineTo(gridPointX(0, range), gridPointY(0, range));
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
        console.log('[Renderer] Isometric grid drawn');
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
        console.log('[Renderer] drawNetwork called with:', network);
        this.clear();
        this.drawGrid();

        if (!network || !network.tracks) {
            console.log('[Renderer] No network or tracks to draw');
            return;
        }
        console.log('[Renderer] Drawing', network.tracks.length, 'tracks');

        // Draw block section overlays first (underneath tracks)
        if (network.block_sections) {
            this.drawBlockSections(network.block_sections, network.tracks);
        }

        // Draw tracks
        network.tracks.forEach(track => {
            this.drawTrack(track);
        });

        // Draw debug overlays (after tracks, before trains)
        this.drawDebugOverlays(network.tracks);

        // Draw trains
        if (network.trains) {
            network.trains.forEach(train => {
                this.drawTrain(train);
            });
        }
    }

    drawBlockSections(blockSections, tracks) {
        // Create a map of track positions to block info
        const trackBlockMap = new Map();

        for (const [blockId, block] of Object.entries(blockSections)) {
            if (block.tracks) {
                for (const trackPos of block.tracks) {
                    const key = `${trackPos[0]},${trackPos[1]}`;
                    trackBlockMap.set(key, {
                        id: blockId,
                        state: block.state || 'FREE'
                    });
                }
            }
        }

        // Draw colored overlays for each track in a block
        tracks.forEach(track => {
            const key = `${track.x},${track.y}`;
            const blockInfo = trackBlockMap.get(key);
            if (blockInfo) {
                const cellSize = this.gridSize * this.scale;
                const screenX = this.gridToScreenX(track.x, track.y);
                const screenY = this.gridToScreenY(track.x, track.y);

                this.ctx.fillStyle = this.blockColors[blockInfo.state] || this.blockColors['FREE'];
                this.ctx.fillRect(screenX + 2, screenY + 2, cellSize - 4, cellSize - 4);
            }
        });
    }

    drawTrack(track) {
        // Convert grid coordinates to screen coordinates (isometric)
        const cellSize = this.gridSize * this.scale;
        const cx = this.gridToScreenX(track.x, track.y);  // center X
        const cy = this.gridToScreenY(track.x, track.y);   // center Y
        const half = cellSize / 2;

        // Get track_bits directly (6-bit system is the source of truth)
        const trackBits = track.track_bits ?? 0;

        this.ctx.save();
        this.ctx.translate(cx, cy);

        // ATC style: determine track color based on state
        let trackColor = '#666'; // Default grey for unoccupied
        if (track.occupied_by) {
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
        } else {
            // Simple track - render all segments
            this.drawTrackFromBits(trackBits, half, trackColor, track.dead_end_ports || []);
        }

        // Draw special elements (signals, stations)
        // Note: TrackType enum values are lowercase ('signal', 'station')
        if (track.type === 'signal') {
            this.drawSignal(track, half);
        } else if (track.type === 'station') {
            this.drawStation(track, half);
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

    // Draw track directly from 6-bit track_bits
    drawTrackFromBits(trackBits, half, color, deadEndPorts = []) {
        const edgePoints = this.getEdgePoints(half);

        this.ctx.strokeStyle = color;
        this.ctx.lineWidth = 4 * this.scale;
        this.ctx.lineCap = 'round';

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
                        this.ctx.beginPath();
                        this.ctx.moveTo(0, 0);
                        this.ctx.lineTo(otherPoint.x, otherPoint.y);
                        this.ctx.stroke();
                    } else {
                        // Diagonal: draw from one edge to other
                        this.ctx.beginPath();
                        this.ctx.moveTo(deadEndPoint.x, deadEndPoint.y);
                        this.ctx.lineTo(otherPoint.x, otherPoint.y);
                        this.ctx.stroke();
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
        if (isCrossing(trackBits) ) {
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

        this.ctx.lineCap = 'round';

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
        
        // Draw all segments with appropriate styling
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

            this.ctx.strokeStyle = isActive ? color : '#444';
            this.ctx.lineWidth = isActive ? 4 * this.scale : 2 * this.scale;

            this.ctx.beginPath();
            this.ctx.moveTo(start.x, start.y);
            this.ctx.lineTo(end.x, end.y);
            this.ctx.stroke();
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

    drawSignal(track, half) {
        // Signal direction arrow and aspect
        const signalDir = track.signal_direction || 'EAST';
        const edgePoints = this.getEdgePoints(half);

        // Draw signal head (enum values are lowercase: 'green', 'red')
        const isGreen = track.signal_state === 'green' || track.signal_state === 'GREEN';
        this.ctx.fillStyle = isGreen ? '#04d361' : '#e83f5b';

        // Position signal head at edge midpoint
        const edge = signalDir.charAt(0).toUpperCase();
        const edgePoint = edgePoints[edge] || { x: 0, y: 0 };
        const sx = edgePoint.x * 0.7;  // Slightly inward from edge
        const sy = edgePoint.y * 0.7;

        // Calculate arrow angle based on direction
        // Arrow points in the direction of travel (toward the edge)
        let arrowAngle = Math.atan2(edgePoint.y, edgePoint.x);

        // Draw signal circle
        this.ctx.beginPath();
        this.ctx.arc(sx, sy - 6, 5, 0, Math.PI * 2);
        this.ctx.fill();

        // Draw direction arrow
        this.ctx.save();
        this.ctx.translate(sx, sy + 6);
        this.ctx.rotate(arrowAngle);
        this.ctx.fillStyle = '#fff';
        this.ctx.beginPath();
        this.ctx.moveTo(6, 0);
        this.ctx.lineTo(-2, -4);
        this.ctx.lineTo(-2, 4);
        this.ctx.closePath();
        this.ctx.fill();
        this.ctx.restore();
    }

    drawStation(track, half) {
        // Station platform - draw as diamond shape matching the tile
        const padding = 4 * this.scale;
        const innerHalf = half - padding;

        // Draw diamond-shaped platform
        this.ctx.fillStyle = '#2a2a3a';
        this.ctx.beginPath();
        this.ctx.moveTo(0, -innerHalf);          // top
        this.ctx.lineTo(innerHalf, 0);           // right
        this.ctx.lineTo(0, innerHalf);           // bottom
        this.ctx.lineTo(-innerHalf, 0);          // left
        this.ctx.closePath();
        this.ctx.fill();

        // Station border
        this.ctx.strokeStyle = '#4a4a5a';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.stroke();

        // Station name/label
        this.ctx.fillStyle = '#fff';
        this.ctx.font = `${9 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        const label = track.station_name ? track.station_name.substring(0, 4) : 'STA';
        this.ctx.fillText(label, 0, 0);
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
            console.log('[Renderer] drawPreviewTracks: no tracks to draw');
            return;
        }

        console.log('[Renderer] drawPreviewTracks:', tracks.length, 'tracks');

        const cellSize = this.gridSize * this.scale;
        const half = cellSize / 2;
        const previewColor = 'rgba(255, 200, 50, 0.8)'; // Light yellow preview color

        for (const track of tracks) {
            const trackBits = track.track_bits ?? 0;
            console.log('[Renderer] Drawing preview track at', track.x, track.y, 'bits:', trackBits);
            if (trackBits === 0) continue; // Skip empty tracks

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);

            this.ctx.save();
            this.ctx.translate(cx, cy);

            // Draw track segments using the same rendering logic
            const edgePoints = this.getEdgePoints(half);
            this.ctx.strokeStyle = previewColor;
            this.ctx.lineWidth = 5 * this.scale; // Slightly thicker for visibility
            this.ctx.lineCap = 'round';

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
}
