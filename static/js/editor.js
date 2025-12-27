// Track bit constants (matching renderer.js)
// Version: 2.0 - Fixed TRACK constants and signal/station tools
// Note: TRACK_BITS and FORBIDDEN_TRACK_BITS are defined in renderer.js (loaded first)
const TRACK_NS = 1;   // 0b000001 - North to South
const TRACK_EW = 2;   // 0b000010 - East to West
const TRACK_NE = 4;   // 0b000100 - North to East
const TRACK_NW = 8;   // 0b001000 - North to West
const TRACK_SE = 16;  // 0b010000 - South to East
const TRACK_SW = 32;  // 0b100000 - South to West

// Convert directed_paths to track_bits
function pathsToTrackBits(directedPaths) {
    const pathMap = {
        'NS': TRACK_NS, 'SN': TRACK_NS,
        'EW': TRACK_EW, 'WE': TRACK_EW,
        'NE': TRACK_NE, 'EN': TRACK_NE,
        'NW': TRACK_NW, 'WN': TRACK_NW,
        'SE': TRACK_SE, 'ES': TRACK_SE,
        'SW': TRACK_SW, 'WS': TRACK_SW
    };

    // Convert full port names to single letters
    const toShort = (port) => port ? port.charAt(0) : '';

    let bits = 0;
    for (const path of directedPaths) {
        const key = toShort(path.port1) + toShort(path.port2);
        bits |= pathMap[key] || 0;
    }
    return bits;
}

// A* pathfinder for construction - prefers diagonal/zigzag paths like Simutrans
class ConstructionPathfinder {
    constructor() {
        // Cost factors - prefer zigzag (diagonal appearance)
        this.costs = {
            useExisting: 0.2, // Using existing track is cheapest
            extend: 0.5,      // Extending existing track direction
            diagonal: 1,      // Alternating direction (zigzag) is preferred
            straight: 2,      // Going straight costs more
            sameTurn: 3,      // Turning same direction twice in a row
            createSwitch: 5,  // Creating a Y-junction (one switch)
            createCrossover: 7, // Creating a crossover (more than 1 switch, less than 2)
            reverse: 100      // Going back on yourself (180 degree)
        };

        // Track bit constants
        this.TRACK_NS = 1;
        this.TRACK_EW = 2;
        this.TRACK_NE = 4;
        this.TRACK_NW = 8;
        this.TRACK_SE = 16;
        this.TRACK_SW = 32;
    }

    // Get the track bit for a path between two ports
    getTrackBitForPath(port1, port2) {
        const key = [port1, port2].sort().join('');
        const pathMap = {
            'NORTHSOUTH': this.TRACK_NS,
            'EASTWEST': this.TRACK_EW,
            'EASTNORTH': this.TRACK_NE,
            'NORTHWEST': this.TRACK_NW,
            'EASTSOUTH': this.TRACK_SE,
            'SOUTHWEST': this.TRACK_SW
        };
        return pathMap[key] || 0;
    }

    // Count number of track segments in track_bits
    countTrackSegments(trackBits) {
        let count = 0;
        if (trackBits & this.TRACK_NS) count++;
        if (trackBits & this.TRACK_EW) count++;
        if (trackBits & this.TRACK_NE) count++;
        if (trackBits & this.TRACK_NW) count++;
        if (trackBits & this.TRACK_SE) count++;
        if (trackBits & this.TRACK_SW) count++;
        return count;
    }

    // Check if two track segments share an endpoint (Y-junction) or cross (crossover)
    // Returns: 'none', 'switch', or 'crossover'
    detectSwitchType(existingBits, newBit) {
        if (!existingBits || !newBit) return 'none';
        if (existingBits & newBit) return 'none'; // Already has this path

        const combined = existingBits | newBit;
        const existingCount = this.countTrackSegments(existingBits);
        const newCount = this.countTrackSegments(combined);

        if (newCount <= existingCount) return 'none'; // No new segment added

        // Check for crossover: NS+EW or NE+SW or NW+SE (no shared endpoints)
        const crossovers = [
            [this.TRACK_NS, this.TRACK_EW],
            [this.TRACK_NE, this.TRACK_SW],
            [this.TRACK_NW, this.TRACK_SE]
        ];

        for (const [a, b] of crossovers) {
            if ((combined & a) && (combined & b)) {
                // Check if this addition creates the crossover
                if ((existingBits & a) && (newBit & b)) return 'crossover';
                if ((existingBits & b) && (newBit & a)) return 'crossover';
            }
        }

        // If we're adding a segment to an existing track, it's a switch
        if (existingCount >= 1 && newCount > existingCount) {
            return 'switch';
        }

        return 'none';
    }

    // Get preferred directions from existing track's track_bits
    getExistingTrackDirections(trackBits) {
        const directions = [];
        // Track bit constants: NS=1, EW=2, NE=4, NW=8, SE=16, SW=32
        if (trackBits & 1) { // NS
            directions.push('NORTH', 'SOUTH');
        }
        if (trackBits & 2) { // EW
            directions.push('EAST', 'WEST');
        }
        if (trackBits & 4) { // NE
            directions.push('NORTH', 'EAST');
        }
        if (trackBits & 8) { // NW
            directions.push('NORTH', 'WEST');
        }
        if (trackBits & 16) { // SE
            directions.push('SOUTH', 'EAST');
        }
        if (trackBits & 32) { // SW
            directions.push('SOUTH', 'WEST');
        }
        return [...new Set(directions)]; // Remove duplicates
    }

    findPath(startX, startY, endX, endY, existingTracks) {
        const start = { x: startX, y: startY };
        const end = { x: endX, y: endY };

        if (startX === endX && startY === endY) {
            return [start];
        }

        const trackMap = new Map();

        // Build map of existing tracks
        if (existingTracks) {
            existingTracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));
        }

        // Check if destination is blocked (station or signal) - return null to signal no path
        const endTrack = trackMap.get(`${endX},${endY}`);
        if (endTrack && (endTrack.type === 'signal' || endTrack.type === 'station')) {
            return null; // Destination is blocked
        }

        // Check if start position has an existing track - prefer extending it
        const startTrack = trackMap.get(`${startX},${startY}`);
        const preferredDirs = startTrack ? this.getExistingTrackDirections(startTrack.track_bits || 0) : [];

        const openSet = [{
            pos: start,
            g: 0,
            h: this.heuristic(start, end),
            f: this.heuristic(start, end),
            parent: null,
            fromDir: null,
            prevTurnDir: null  // Track if we turned left or right last time
        }];
        const closedSet = new Set();

        while (openSet.length > 0) {
            // Get lowest f score
            openSet.sort((a, b) => a.f - b.f);
            const current = openSet.shift();
            const key = `${current.pos.x},${current.pos.y}`;
            
            if (current.pos.x === end.x && current.pos.y === end.y) {
                const path = this.reconstructPath(current);
                return this.alignZigzagTails(path, end);
            }
            
            closedSet.add(key);

            // Explore neighbors (4 directions)
            const neighbors = [
                { x: current.pos.x, y: current.pos.y + 1, dir: 'NORTH' },
                { x: current.pos.x + 1, y: current.pos.y, dir: 'EAST' },
                { x: current.pos.x, y: current.pos.y - 1, dir: 'SOUTH' },
                { x: current.pos.x - 1, y: current.pos.y, dir: 'WEST' }
            ];

            for (const neighbor of neighbors) {
                const nKey = `${neighbor.x},${neighbor.y}`;
                if (closedSet.has(nKey)) continue;

                // Skip tiles with stations or signals (they are obstacles)
                const existingTrack = trackMap.get(nKey);
                if (existingTrack && (existingTrack.type === 'signal' || existingTrack.type === 'station')) {
                    continue;
                }

                // Calculate cost
                let moveCost = this.costs.straight;
                let turnDir = null;

                // Check if neighbor has existing track we can use
                if (existingTrack && existingTrack.track_bits) {
                    const trackDirs = this.getExistingTrackDirections(existingTrack.track_bits);
                    // We enter from opposite direction of our move
                    const entryDir = this.getOppositeDir(neighbor.dir);
                    if (trackDirs.includes(entryDir)) {
                        // This existing track has a path from our entry direction - use it!
                        moveCost = this.costs.useExisting;
                    }
                } else if (!current.fromDir) {
                    // First move from start - prefer extending existing track direction
                    if (preferredDirs.length > 0 && preferredDirs.includes(neighbor.dir)) {
                        moveCost = this.costs.extend; // Cheapest - extend existing track
                    }
                } else if (this.isReverse(current.fromDir, neighbor.dir)) {
                    // Don't go backwards
                    moveCost = this.costs.reverse;
                } else if (current.fromDir !== neighbor.dir) {
                    // We're turning - is it the same direction as last turn?
                    turnDir = this.getTurnDirection(current.fromDir, neighbor.dir);
                    if (current.prevTurnDir && turnDir === current.prevTurnDir) {
                        // Same turn direction = S-curve, costs more
                        moveCost = this.costs.sameTurn;
                    } else {
                        // Alternating turns = zigzag, preferred!
                        moveCost = this.costs.diagonal;
                    }
                }

                // Check if this move creates a switch on the CURRENT tile
                // (we're adding a path from current.fromDir to neighbor.dir)
                if (current.fromDir) {
                    const currentTileTrack = trackMap.get(key);
                    const currentBits = currentTileTrack?.track_bits || 0;
                    const newPathBit = this.getTrackBitForPath(current.fromDir, neighbor.dir);
                    const switchType = this.detectSwitchType(currentBits, newPathBit);
                    if (switchType === 'crossover') {
                        moveCost += this.costs.createCrossover;
                    } else if (switchType === 'switch') {
                        moveCost += this.costs.createSwitch;
                    }
                }

                const g = current.g + moveCost;
                const h = this.heuristic(neighbor, end);
                const f = g + h;

                // Check if this path is better
                const existing = openSet.find(n => n.pos.x === neighbor.x && n.pos.y === neighbor.y);
                if (existing) {
                    if (g < existing.g) {
                        existing.g = g;
                        existing.f = f;
                        existing.parent = current;
                        existing.fromDir = neighbor.dir;
                        existing.prevTurnDir = turnDir || current.prevTurnDir;
                    }
                } else {
                    openSet.push({
                        pos: { x: neighbor.x, y: neighbor.y },
                        g, h, f,
                        parent: current,
                        fromDir: neighbor.dir,
                        prevTurnDir: turnDir || current.prevTurnDir
                    });
                }
            }
        }

        // No path found - return diagonal line
        return this.diagonalLine(start, end);
    }

    heuristic(a, b) {
        // Chebyshev distance - allows diagonal preference
        return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y));
    }

    isReverse(dir1, dir2) {
        const opposites = {
            'NORTH': 'SOUTH', 'SOUTH': 'NORTH',
            'EAST': 'WEST', 'WEST': 'EAST'
        };
        return opposites[dir1] === dir2;
    }

    getOppositeDir(dir) {
        const opposites = {
            'NORTH': 'SOUTH', 'SOUTH': 'NORTH',
            'EAST': 'WEST', 'WEST': 'EAST'
        };
        return opposites[dir];
    }

    getTurnDirection(fromDir, toDir) {
        // Returns 'LEFT' or 'RIGHT' based on turn
        const turns = {
            'NORTH': { 'EAST': 'RIGHT', 'WEST': 'LEFT' },
            'EAST': { 'SOUTH': 'RIGHT', 'NORTH': 'LEFT' },
            'SOUTH': { 'WEST': 'RIGHT', 'EAST': 'LEFT' },
            'WEST': { 'NORTH': 'RIGHT', 'SOUTH': 'LEFT' }
        };
        return turns[fromDir]?.[toDir] || null;
    }

    reconstructPath(node) {
        const path = [];
        let current = node;
        while (current) {
            path.unshift(current.pos);
            current = current.parent;
        }
        return path;
    }

    diagonalLine(start, end) {
        // Create a zigzag path that alternates X and Y movement
        const path = [{ x: start.x, y: start.y }];
        let x = start.x;
        let y = start.y;
        const dx = end.x > start.x ? 1 : (end.x < start.x ? -1 : 0);
        const dy = end.y > start.y ? 1 : (end.y < start.y ? -1 : 0);

        let toggle = true;
        while (x !== end.x || y !== end.y) {
            if (toggle && x !== end.x) {
                x += dx;
            } else if (!toggle && y !== end.y) {
                y += dy;
            } else if (x !== end.x) {
                x += dx;
            } else if (y !== end.y) {
                y += dy;
            }
            path.push({ x, y });
            toggle = !toggle;
        }

        return path;
    }

    // Detect the zigzag pattern from path moves (e.g., ['NORTH', 'EAST'] for NE zigzag)
    detectZigzagPattern(path) {
        if (path.length < 4) return null;

        // Get directions for each move
        const directions = [];
        for (let i = 1; i < path.length; i++) {
            const dx = path[i].x - path[i-1].x;
            const dy = path[i].y - path[i-1].y;
            if (dy > 0) directions.push('NORTH');
            else if (dy < 0) directions.push('SOUTH');
            else if (dx > 0) directions.push('EAST');
            else if (dx < 0) directions.push('WEST');
        }

        // Look at middle 60% of moves to find pattern
        const start = Math.floor(directions.length * 0.2);
        const end = Math.floor(directions.length * 0.8);
        const middle = directions.slice(start, end);

        if (middle.length < 2) return null;

        // Find two alternating directions
        const uniqueDirs = [...new Set(middle)];
        if (uniqueDirs.length !== 2) return null;

        // Verify they alternate
        let alternates = true;
        for (let i = 1; i < middle.length && alternates; i++) {
            if (middle[i] === middle[i-1]) alternates = false;
        }

        return alternates ? uniqueDirs : null;
    }

    // Align first and last tiles with the zigzag pattern
    alignZigzagTails(path, end) {
        if (path.length < 3) return path;

        const pattern = this.detectZigzagPattern(path);
        if (!pattern) return path;

        let result = [...path];

        // Adjust first tile: ensure first move aligns with pattern
        if (result.length >= 2) {
            const firstMove = {
                dx: result[1].x - result[0].x,
                dy: result[1].y - result[0].y
            };
            const firstDir = firstMove.dy > 0 ? 'NORTH' : (firstMove.dy < 0 ? 'SOUTH' : (firstMove.dx > 0 ? 'EAST' : 'WEST'));

            // If first move is not in pattern, insert an intermediate tile
            if (!pattern.includes(firstDir)) {
                // Pick the pattern direction that gets us closer to end
                const p0 = pattern[0];
                const delta0 = this.getDelta(p0);
                const newPos0 = { x: result[0].x + delta0.dx, y: result[0].y + delta0.dy };
                const dist0 = Math.abs(newPos0.x - end.x) + Math.abs(newPos0.y - end.y);

                const p1 = pattern[1];
                const delta1 = this.getDelta(p1);
                const newPos1 = { x: result[0].x + delta1.dx, y: result[0].y + delta1.dy };
                const dist1 = Math.abs(newPos1.x - end.x) + Math.abs(newPos1.y - end.y);

                const chosenDelta = dist0 <= dist1 ? delta0 : delta1;
                const insertPos = { x: result[0].x + chosenDelta.dx, y: result[0].y + chosenDelta.dy };

                // Only insert if it doesn't go backwards
                if (insertPos.x !== result[1].x || insertPos.y !== result[1].y) {
                    result.splice(1, 0, insertPos);
                }
            }
        }

        // Adjust last tile: ensure last move aligns with pattern
        if (result.length >= 2) {
            const lastIdx = result.length - 1;
            const lastMove = {
                dx: result[lastIdx].x - result[lastIdx - 1].x,
                dy: result[lastIdx].y - result[lastIdx - 1].y
            };
            const lastDir = lastMove.dy > 0 ? 'NORTH' : (lastMove.dy < 0 ? 'SOUTH' : (lastMove.dx > 0 ? 'EAST' : 'WEST'));

            // If last move is not in pattern, insert an intermediate tile before end
            if (!pattern.includes(lastDir) && result.length >= 3) {
                // Pick the pattern direction that comes from the previous tile toward end
                const prevPos = result[lastIdx - 1];
                const p0 = pattern[0];
                const delta0 = this.getDelta(p0);
                const penultimate0 = { x: end.x - delta0.dx, y: end.y - delta0.dy };

                const p1 = pattern[1];
                const delta1 = this.getDelta(p1);
                const penultimate1 = { x: end.x - delta1.dx, y: end.y - delta1.dy };

                // Pick the one closer to prevPos
                const dist0 = Math.abs(penultimate0.x - prevPos.x) + Math.abs(penultimate0.y - prevPos.y);
                const dist1 = Math.abs(penultimate1.x - prevPos.x) + Math.abs(penultimate1.y - prevPos.y);

                const insertPos = dist0 <= dist1 ? penultimate0 : penultimate1;

                // Only insert if it's different from existing tiles
                if ((insertPos.x !== prevPos.x || insertPos.y !== prevPos.y) &&
                    (insertPos.x !== end.x || insertPos.y !== end.y)) {
                    result.splice(lastIdx, 0, insertPos);
                }
            }
        }

        return result;
    }

    getDelta(dir) {
        switch (dir) {
            case 'NORTH': return { dx: 0, dy: 1 };
            case 'SOUTH': return { dx: 0, dy: -1 };
            case 'EAST': return { dx: 1, dy: 0 };
            case 'WEST': return { dx: -1, dy: 0 };
            default: return { dx: 0, dy: 0 };
        }
    }
}

// Convert path to track data with directed_paths format
// Each path has: {port1, port2, direction: 'both'|'forward'|'reverse'}
// Direction is determined by drag direction: port1→port2 is 'forward'
function pathToTracks(path, cachedNetwork) {
    if (!path || path.length === 0) return [];
    
    const tracksToUpdate = new Map(); // key -> {x, y, directed_paths: [{port1, port2, direction}, ...]}
    const existingTracks = new Map();
    
    // Build a map of existing tracks if we have network data
    if (cachedNetwork && cachedNetwork.tracks) {
        for (const track of cachedNetwork.tracks) {
            existingTracks.set(`${track.x},${track.y}`, track);
        }
    }
    
    // Helper to check if a neighbor has a port pointing toward a position
    function neighborHasPortTo(neighborX, neighborY, portName) {
        const neighborTrack = existingTracks.get(`${neighborX},${neighborY}`);
        if (!neighborTrack) return false;
        
        // Check directed_paths
        if (neighborTrack.directed_paths) {
            return neighborTrack.directed_paths.some(p => 
                p.port1 === portName || p.port2 === portName
            );
        }
        // Legacy: check paths array
        if (neighborTrack.paths) {
            return neighborTrack.paths.some(p => p.includes(portName));
        }
        // Legacy: check connections
        if (neighborTrack.connections) {
            return portName in neighborTrack.connections;
        }
        return false;
    }
    
    // Helper to add a directed path to a track in our update map
    function addPath(x, y, port1, port2, direction = 'both') {
        const key = `${x},${y}`;
        if (!tracksToUpdate.has(key)) {
            const existing = existingTracks.get(key);
            tracksToUpdate.set(key, {
                x, y,
                type: existing?.type || 'STRAIGHT',
                directed_paths: []
            });
        }
        const track = tracksToUpdate.get(key);
        
        // Check if path already exists (in either direction)
        const pathExists = track.directed_paths.some(p => 
            (p.port1 === port1 && p.port2 === port2) ||
            (p.port1 === port2 && p.port2 === port1)
        );
        if (!pathExists && port1 !== port2) {
            track.directed_paths.push({ port1, port2, direction });
        }
    }
    
    // Process each cell in the path
    for (let i = 0; i < path.length; i++) {
        const current = path[i];
        const prev = i > 0 ? path[i - 1] : null;
        const next = i < path.length - 1 ? path[i + 1] : null;
        
        // Direction we came FROM (port where train enters)
        const entryPort = prev ? getDirection(current, prev) : null;
        // Direction we go TO (port where train exits)
        const exitPort = next ? getDirection(current, next) : null;
        
        // Interior cells: simple entry→exit path
        if (prev && next) {
            // Direction: from entry to exit (forward direction of drag)
            addPath(current.x, current.y, entryPort, exitPort, 'both');
            continue;
        }
        
        // First cell (no prev): connect to existing neighbor or create endpoint
        if (!prev && next) {
            const exitDir = getDirection(current, next);
            const entryDir = getOpposite(exitDir);
            
            // Check all 4 neighbors for existing connections
            const neighbors = [
                { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
                { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
                { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
                { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' }
            ];
            
            let foundConnection = false;
            for (const n of neighbors) {
                if (n.myPort === exitDir) continue; // Skip direction we're going
                
                if (neighborHasPortTo(current.x + n.dx, current.y + n.dy, n.theirPort)) {
                    addPath(current.x, current.y, n.myPort, exitDir, 'both');
                    foundConnection = true;
                }
            }
            
            // If no existing connection, create a through-track from opposite direction
            if (!foundConnection) {
                addPath(current.x, current.y, entryDir, exitDir, 'both');
            }
            continue;
        }
        
        // Last cell (no next): connect to existing neighbor or create endpoint
        if (prev && !next) {
            const entryDir = getDirection(current, prev);
            const exitDir = getOpposite(entryDir);
            
            // Check all 4 neighbors for existing connections
            const neighbors = [
                { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
                { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
                { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
                { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' }
            ];
            
            let foundConnection = false;
            for (const n of neighbors) {
                if (n.myPort === entryDir) continue; // Skip direction we came from
                
                if (neighborHasPortTo(current.x + n.dx, current.y + n.dy, n.theirPort)) {
                    addPath(current.x, current.y, entryDir, n.myPort, 'both');
                    foundConnection = true;
                }
            }
            
            // If no existing connection, create a through-track to opposite direction
            if (!foundConnection) {
                addPath(current.x, current.y, entryDir, exitDir, 'both');
            }
            continue;
        }
        
        // Single cell (no prev and no next): shouldn't happen, but handle it
        if (!prev && !next) {
            // Create a default E-W track
            addPath(current.x, current.y, 'WEST', 'EAST', 'both');
        }
    }
    
    // Convert map to array and add track_bits
    const tracks = [];
    for (const track of tracksToUpdate.values()) {
        // Convert directed_paths to track_bits
        const trackBits = pathsToTrackBits(track.directed_paths);

        // Validate against forbidden configurations
        if (FORBIDDEN_TRACK_BITS.has(trackBits)) {
            console.warn(`[pathToTracks] Skipping forbidden configuration (${trackBits}) at (${track.x}, ${track.y})`);
            // Show error to user
            if (window.showError) {
                window.showError(`Cannot create forbidden configuration (${trackBits}). This would require a 3-way switch.`);
            }
            continue;  // Skip this track
        }

        // Add track_bits to track data
        track.track_bits = trackBits;
        console.log('[pathToTracks] pos:', {x: track.x, y: track.y}, 'track_bits:', trackBits, 'directed_paths:', track.directed_paths);
        tracks.push(track);
    }

    return tracks;
}

function getDirection(from, to) {
    if (to.y > from.y) return 'NORTH';
    if (to.y < from.y) return 'SOUTH';
    if (to.x > from.x) return 'EAST';
    if (to.x < from.x) return 'WEST';
    return null;
}

function getOpposite(dir) {
    switch (dir) {
        case 'NORTH': return 'SOUTH';
        case 'SOUTH': return 'NORTH';
        case 'EAST': return 'WEST';
        case 'WEST': return 'EAST';
        default: return null;
    }
}

class Editor {
    constructor(renderer, api) {
        console.log('[Editor] Initializing...');
        this.renderer = renderer;
        this.api = api;
        this.selectedTool = 'select';
        this.isPanning = false;
        this.isDraggingBuild = false;
        this.dragStart = null;
        this.dragEnd = null;
        this.previewPath = null;
        this.lastValidPreviewPath = null;
        this.lastValidPreviewTracks = null;
        this.lastMouseX = 0;
        this.lastMouseY = 0;
        this.selectedElement = null;
        this.cachedNetwork = null;
        
        this.pathfinder = new ConstructionPathfinder();
        this.setupEventListeners();
        console.log('[Editor] Initialization complete');
    }

    setupEventListeners() {
        console.log('[Editor] Setting up event listeners...');
        
        // Tool selection
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');
                this.selectedTool = e.currentTarget.dataset.tool;
                this.previewPath = null;
                console.log('[Editor] Selected tool:', this.selectedTool);
            });
        });

        const canvas = this.renderer.canvas;
        console.log('[Editor] Canvas element:', canvas);

        // Mouse down - start drag or pan
        canvas.addEventListener('mousedown', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);
            console.log('[Editor] Mouse down at grid:', gridPos, 'button:', e.button, 'tool:', this.selectedTool);

            // Middle mouse or select tool = pan
            if (e.button === 1 || (e.button === 0 && this.selectedTool === 'select')) {
                console.log('[Editor] Starting pan');
                this.isPanning = true;
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            } 
            // Left click with build tool = start drag build
            else if (e.button === 0 && ['track', 'curve', 'switch', 'signal', 'station'].includes(this.selectedTool)) {
                console.log('[Editor] Starting drag build');
                this.isDraggingBuild = true;
                this.dragStart = gridPos;
                this.dragEnd = gridPos;
                // Clear cached path for new drag operation
                this.lastValidPreviewPath = null;
                this.lastValidPreviewTracks = null;
            }
            // Left click with eraser = erase
            else if (e.button === 0 && this.selectedTool === 'eraser') {
                console.log('[Editor] Erasing at', gridPos);
                this.handleErase(gridPos);
            }
        });

        // Mouse move - update drag preview or pan
        canvas.addEventListener('mousemove', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);
            
            document.getElementById('status-coords').textContent = `${gridPos.x}, ${gridPos.y}`;

            if (this.isPanning) {
                const dx = e.clientX - this.lastMouseX;
                const dy = e.clientY - this.lastMouseY;
                this.renderer.panX += dx;
                this.renderer.panY += dy;
                this.renderer.draw();
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            } 
            else if (this.isDraggingBuild && this.dragStart) {
                this.dragEnd = gridPos;
                this.updatePreviewPath();
            }
        });

        // Mouse up - commit drag build
        canvas.addEventListener('mouseup', async (e) => {
            console.log('[Editor] Mouse up, isPanning:', this.isPanning, 'isDraggingBuild:', this.isDraggingBuild);
            if (this.isPanning) {
                this.isPanning = false;
            }
            else if (this.isDraggingBuild && this.dragStart && this.dragEnd) {
                console.log('[Editor] Committing drag build from', this.dragStart, 'to', this.dragEnd);
                await this.commitDragBuild();
                this.isDraggingBuild = false;
                this.dragStart = null;
                this.dragEnd = null;
                this.previewPath = null;
            }
        });

        // Right click - context actions (rotate signal, toggle switch)
        canvas.addEventListener('contextmenu', async (e) => {
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);
            console.log('[Editor] Right click at', gridPos);
            
            await this.handleRightClick(gridPos);
        });

        // Zoom
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const zoomIntensity = 0.1;
            const delta = e.deltaY < 0 ? 1 : -1;
            const newScale = this.renderer.scale + (delta * zoomIntensity);

            if (newScale > 0.1 && newScale < 5) {
                this.renderer.scale = newScale;
                this.renderer.draw();
                document.getElementById('status-zoom').textContent = `${Math.round(newScale * 100)}%`;
            }
        });

        // Click to select element for properties panel
        canvas.addEventListener('click', async (e) => {
            // Only handle selection with select tool
            if (this.selectedTool !== 'select') return;
            
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);
            
            this.selectElement(gridPos);
        });
    }

    updatePreviewPath() {
        if (!this.dragStart || !this.dragEnd) return;

        if (this.selectedTool === 'track') {
            // Use pathfinder for track placement only
            const existingTracks = this.cachedNetwork ? this.cachedNetwork.tracks : [];
            const foundPath = this.pathfinder.findPath(
                this.dragStart.x, this.dragStart.y,
                this.dragEnd.x, this.dragEnd.y,
                existingTracks
            );

            // If path found, use it and cache it; otherwise use last valid path
            if (foundPath) {
                this.previewPath = foundPath;
                this.previewTracks = pathToTracks(this.previewPath, this.cachedNetwork);
                // Cache this valid path
                this.lastValidPreviewPath = this.previewPath;
                this.lastValidPreviewTracks = this.previewTracks;
            } else {
                // Destination blocked - use last valid path
                this.previewPath = this.lastValidPreviewPath;
                this.previewTracks = this.lastValidPreviewTracks;
            }
            console.log('[Editor] Preview path:', this.previewPath?.length, 'tiles, preview tracks:', this.previewTracks?.length);
        } else {
            // Single placement for signal, station, and other tools
            this.previewPath = [this.dragEnd];
            this.previewTracks = null;
        }

        // Redraw with preview
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
            // Draw actual track geometry preview if available, otherwise fall back to path preview
            if (this.previewTracks && this.previewTracks.length > 0) {
                this.renderer.drawPreviewTracks(this.previewTracks);
            } else if (this.previewPath) {
                this.renderer.drawPreviewPath(this.previewPath);
            }
        }
    }

    async commitDragBuild() {
        console.log('[Editor] commitDragBuild called, previewPath:', this.previewPath);
        if (!this.previewPath || this.previewPath.length === 0) {
            console.log('[Editor] No preview path, skipping');
            return;
        }
        
        try {
            if (this.selectedTool === 'track') {
                const tracks = pathToTracks(this.previewPath, this.cachedNetwork);
                console.log('[Editor] Adding tracks:', tracks);
                await this.api.addTracksBulk(tracks);
            } 
            else if (this.selectedTool === 'signal') {
                // Add signal to existing track at click position
                const pos = this.previewPath[0];
                const existingTrack = this.cachedNetwork?.tracks?.find(t => t.x === pos.x && t.y === pos.y);
                
                if (!existingTrack) {
                    window.showError('Cannot place signal: No track at this position');
                    return;
                }
                if (existingTrack.is_switch) {
                    window.showError('Cannot place signal on a switch');
                    return;
                }
                if (existingTrack.station_name) {
                    window.showError('Cannot place signal on a station');
                    return;
                }
                
                console.log('[Editor] Adding signal at', pos);
                await this.api.updateTrack(pos.x, pos.y, {
                    signal_state: 'green',
                    signal_direction: 'NORTH'
                });
            }
            else if (this.selectedTool === 'station') {
                // Add station to existing track at click position
                const pos = this.previewPath[0];
                const existingTrack = this.cachedNetwork?.tracks?.find(t => t.x === pos.x && t.y === pos.y);
                
                if (!existingTrack) {
                    window.showError('Cannot place station: No track at this position');
                    return;
                }
                if (existingTrack.is_switch) {
                    window.showError('Cannot place station on a switch');
                    return;
                }
                if (existingTrack.signal_state) {
                    window.showError('Cannot place station on a signal');
                    return;
                }
                
                const stationName = prompt('Enter station name:', 'Station');
                if (!stationName) return;
                
                console.log('[Editor] Adding station at', pos);
                await this.api.updateTrack(pos.x, pos.y, {
                    station_name: stationName
                });
            }
            else if (this.selectedTool === 'curve') {
                console.log('[Editor] Adding curve at', this.dragEnd);
                await this.api.addTrack({
                    x: this.dragEnd.x,
                    y: this.dragEnd.y,
                    type: 'CURVE',
                    connections: {
                        'SOUTH': ['EAST'],
                        'EAST': ['SOUTH']
                    }
                });
            }
            else if (this.selectedTool === 'switch') {
                console.log('[Editor] Adding switch at', this.dragEnd);
                await this.api.addTrack({
                    x: this.dragEnd.x,
                    y: this.dragEnd.y,
                    type: 'SWITCH',
                    switch_position: 'MAIN',
                    connections: {
                        'WEST': ['EAST', 'SOUTH'],
                        'EAST': ['WEST'],
                        'SOUTH': ['WEST']
                    }
                });
            }
            
            console.log('[Editor] Build committed, refreshing network');
            window.app.refreshNetwork();
        } catch (err) {
            console.error('[Editor] Failed to commit build:', err);
        }
    }

    async handleErase(gridPos) {
        console.log('[Editor] handleErase at', gridPos);
        try {
            await this.api.deleteTrack(gridPos.x, gridPos.y);
            console.log('[Editor] Erase successful, refreshing');
            window.app.refreshNetwork();
        } catch (err) {
            console.error('[Editor] Failed to erase:', err);
        }
    }

    async handleRightClick(gridPos) {
        // Get track at position
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (track.error) return;

            // Check if track has switch points (uses is_switch from simplified global state system)
            if (track.is_switch) {
                // Toggle global switch state between MAIN (0) and DIVERGE (1)
                const newState = track.switch_state === 0 ? 1 : 0;
                console.log(`[Editor] Toggling switch at (${gridPos.x}, ${gridPos.y}) to ${newState === 0 ? 'MAIN' : 'DIVERGE'}`);
                await this.api.updateTrack(gridPos.x, gridPos.y, {
                    switch_state: newState
                });
                await window.app.refreshNetwork();
                // Refresh properties panel to show updated switch state
                await this.selectElement(gridPos);
            }
            else if (track.type === 'SIGNAL') {
                // Get edges that have paths on this track
                const trackBits = track.track_bits ?? 0;
                const connectedEdges = [];

                // Check which edges are connected by track_bits
                // NS=1 connects N,S; EW=2 connects E,W; NE=4 connects N,E; etc.
                if (trackBits & 1) { connectedEdges.push('N', 'S'); }  // NS
                if (trackBits & 2) { connectedEdges.push('E', 'W'); }  // EW
                if (trackBits & 4) { connectedEdges.push('N', 'E'); }  // NE
                if (trackBits & 8) { connectedEdges.push('N', 'W'); }  // NW
                if (trackBits & 16) { connectedEdges.push('S', 'E'); } // SE
                if (trackBits & 32) { connectedEdges.push('S', 'W'); } // SW

                // Deduplicate edges
                const uniqueEdges = [...new Set(connectedEdges)];

                // Map edges to directions
                const edgeToDir = { 'N': 'NORTH', 'E': 'EAST', 'S': 'SOUTH', 'W': 'WEST' };
                const validDirections = uniqueEdges.map(e => edgeToDir[e]);

                if (validDirections.length === 0) {
                    console.log('[Editor] No valid directions for signal');
                    return;
                }

                // Find current direction and cycle to next valid one
                const currentDir = track.signal_direction || validDirections[0];
                const currentIdx = validDirections.indexOf(currentDir);
                const newIdx = currentIdx >= 0 ? (currentIdx + 1) % validDirections.length : 0;
                const newDir = validDirections[newIdx];

                console.log(`[Editor] Rotating signal at (${gridPos.x}, ${gridPos.y}) from ${currentDir} to ${newDir} (valid: ${validDirections.join(', ')})`);
                await this.api.updateTrack(gridPos.x, gridPos.y, {
                    signal_direction: newDir
                });
                window.app.refreshNetwork();
            }
        } catch (err) {
            console.error('Right-click action failed:', err);
        }
    }

    async selectElement(gridPos) {
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (!track.error) {
                this.selectedElement = track;
                this.updatePropertiesPanel(track);
            } else {
                // Create virtual tile for empty position
                this.selectedElement = {
                    x: gridPos.x,
                    y: gridPos.y,
                    track_bits: 0,
                    directed_paths: [],
                    edges: [],
                    isVirtual: true  // Flag to indicate this tile doesn't exist yet
                };
                this.updatePropertiesPanel(this.selectedElement);
            }
        } catch (err) {
            // On network error, also create virtual tile
            this.selectedElement = {
                x: gridPos.x,
                y: gridPos.y,
                track_bits: 0,
                directed_paths: [],
                edges: [],
                isVirtual: true
            };
            this.updatePropertiesPanel(this.selectedElement);
        }
    }

    updatePropertiesPanel(track) {
        const panel = document.querySelector('.properties-panel');
        if (!panel) return;
        
        // Get track_bits directly (6-bit system is the source of truth)
        const trackBits = track.track_bits ?? 0;
        const edges = track.edges || [];
        
        // Track bits segment names
        const SEGMENT_NAMES = {
            1: 'NS', 2: 'EW', 4: 'NE', 8: 'NW', 16: 'SE', 32: 'SW'
        };
        
        // Get active segments as readable list
        const activeSegments = [];
        for (const [bit, name] of Object.entries(SEGMENT_NAMES)) {
            if (trackBits & parseInt(bit)) {
                activeSegments.push(name);
            }
        }
        
        // Determine track shape description
        const numSegments = activeSegments.length;
        let shapeDesc = 'Empty';
        if (numSegments === 1) {
            if (trackBits === 1 || trackBits === 2) shapeDesc = 'Straight';
            else shapeDesc = 'Diagonal';
        } else if (numSegments === 2) {
            if (trackBits === 3) shapeDesc = 'Crossing (NS+EW)';
            else if (trackBits === 36) shapeDesc = 'Diamond (NE+SW)';
            else if (trackBits === 24) shapeDesc = 'Diamond (NW+SE)';
            else shapeDesc = 'Switch';
        } else if (numSegments >= 3) {
            shapeDesc = 'Complex Junction';
        }
        
        // Check if forbidden
        const isForbidden = FORBIDDEN_TRACK_BITS.has(trackBits);
        
        // Build the track bits editor canvas
        const editorId = `track-editor-${Date.now()}`;
        
        panel.innerHTML = `
            <h3>Properties</h3>
            ${track.isVirtual ? `
            <div class="property-item" style="background: #3a2a1a; padding: 8px; border-radius: 4px; margin-bottom: 8px; border-left: 3px solid #ff9900;">
                <span style="color: #ff9900; font-weight: bold;">New Tile (not yet saved)</span>
            </div>
            ` : ''}
            <div class="property-item">
                <label>Position:</label>
                <span>${track.x}, ${track.y}</span>
            </div>
            <div class="property-item">
                <label>Type:</label>
                <span>${track.isVirtual ? 'Empty' : track.type}</span>
            </div>
            
            <!-- Track Bits Display (Primary) -->
            <div class="property-item" style="background: #2a3a2e; padding: 10px; border-radius: 4px; margin: 8px 0;">
                <label style="color: #00ff88; font-weight: bold;">Track Bits:</label>
                <span style="font-family: monospace; font-size: 16px;">${trackBits}</span>
                <span style="color: #888; margin-left: 8px;">(0b${trackBits.toString(2).padStart(6, '0')})</span>
                <div style="font-size: 12px; color: #ffd700; margin-top: 4px;">${shapeDesc}</div>
                <div style="font-size: 11px; color: #ccc; margin-top: 4px;">
                    ${activeSegments.length > 0 ? 'Active: ' + activeSegments.join(', ') : 'No active segments'}
                </div>
                <div style="font-size: 10px; color: #888; margin-top: 4px;">
                    Edges: ${edges.length > 0 ? edges.join(', ') : 'None'} | Segments: ${numSegments}
                </div>
                ${isForbidden ? 
                    '<div style="font-size: 11px; color: #e83f5b; margin-top: 4px; font-weight: bold;">⚠ FORBIDDEN CONFIGURATION</div>' 
                    : ''}
            </div>

            <!-- Track Bits Editor -->
            <div id="bit-editor" style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin: 8px 0;">
                <h4 style="color: #00ff88; font-size: 12px; margin: 0 0 10px 0;">Track Bits Editor</h4>
                <div class="bit-toggles" style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 8px 0;">
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 1 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 1 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="1" ${trackBits & 1 ? 'checked' : ''} style="margin-right: 6px;" /> 
                        <span style="color: ${trackBits & 1 ? '#00ff88' : '#ccc'};">NS (1) ╲</span>
                    </label>
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 2 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 2 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="2" ${trackBits & 2 ? 'checked' : ''} style="margin-right: 6px;" />
                        <span style="color: ${trackBits & 2 ? '#00ff88' : '#ccc'};">EW (2) ╱</span>
                    </label>
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 4 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 4 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="4" ${trackBits & 4 ? 'checked' : ''} style="margin-right: 6px;" />
                        <span style="color: ${trackBits & 4 ? '#00ff88' : '#ccc'};">NE (4) ⌐</span>
                    </label>
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 8 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 8 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="8" ${trackBits & 8 ? 'checked' : ''} style="margin-right: 6px;" />
                        <span style="color: ${trackBits & 8 ? '#00ff88' : '#ccc'};">NW (8) ⌐</span>
                    </label>
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 16 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 16 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="16" ${trackBits & 16 ? 'checked' : ''} style="margin-right: 6px;" />
                        <span style="color: ${trackBits & 16 ? '#00ff88' : '#ccc'};">SE (16) ⌐</span>
                    </label>
                    <label style="display: flex; align-items: center; padding: 6px; background: ${trackBits & 32 ? '#2a4a3e' : '#2a2a34'}; border-radius: 4px; cursor: pointer; font-size: 11px; border: 1px solid ${trackBits & 32 ? '#00ff88' : 'transparent'};">
                        <input type="checkbox" data-bit="32" ${trackBits & 32 ? 'checked' : ''} style="margin-right: 6px;" />
                        <span style="color: ${trackBits & 32 ? '#00ff88' : '#ccc'};">SW (32) ⌐</span>
                    </label>
                </div>
                <div class="bit-info" style="margin: 10px 0; padding: 6px; background: #2a2a34; border-radius: 4px; font-size: 11px;">
                    <p style="margin: 0;">Total: <span id="bit-total" style="font-weight: bold; color: #00ff88;">${trackBits}</span> (0b<span id="bit-binary">${trackBits.toString(2).padStart(6, '0')}</span>)</p>
                    <p class="bit-warning" style="margin: 4px 0 0 0; color: #e83f5b; font-weight: bold; display: ${isForbidden ? 'block' : 'none'};">
                        ⚠ Warning: This is a forbidden configuration!
                    </p>
                </div>
                <button id="apply-bits" class="btn-primary" style="width: 100%; padding: 8px; background: #8257e5; border: none; border-radius: 4px; color: white; font-weight: bold; cursor: pointer;">Apply</button>
            </div>

            <!-- Visual Track Editor Canvas -->
            <div class="property-item" style="background: #1a1a2e; padding: 10px; border-radius: 4px; margin: 8px 0;">
                <label style="color: #00ff88; font-weight: bold; margin-bottom: 8px; display: block;">Visual Editor: (Click segments to toggle)</label>
                <canvas id="${editorId}" width="200" height="200" style="display: block; margin: 0 auto; cursor: pointer; border: 1px solid #444; border-radius: 4px; background: #0a0a14;"></canvas>
            </div>
            
            ${track.switch_points && Object.keys(track.switch_points).length > 0 ? `
            <div class="property-item" style="background: #2a2a3e; padding: 8px; border-radius: 4px; margin: 8px 0;">
                <label style="color: #00d4ff; font-weight: bold;">Switch State:</label>
                ${track.is_diagonal_only_switch ? `
                <div style="margin: 4px 0; padding: 4px 8px; background: #3a2a1a; border-radius: 4px; border-left: 3px solid #ff9900;">
                    <span style="font-size: 10px; color: #ff9900;">⟳ Diagonal-only switch (uses clockwise ordering)</span>
                </div>
                ` : ''}
                <div style="margin: 6px 0; padding: 8px; background: ${track.switch_state === 0 ? '#1a3a2a' : '#3a3a1a'}; border-radius: 4px; border: 2px solid ${track.switch_state === 0 ? '#04d361' : '#ffd93d'};">
                    <span style="font-size: 14px; font-weight: bold; color: ${track.switch_state === 0 ? '#04d361' : '#ffd93d'};">
                        ${track.switch_state === 0 ? 'MAIN (0)' : 'DIVERGE (1)'}
                    </span>
                    <div style="font-size: 10px; color: #aaa; margin-top: 4px;">
                        ${track.is_diagonal_only_switch
                            ? (track.switch_state === 0 ? 'Clockwise first exit' : 'Clockwise second exit')
                            : (track.switch_state === 0 ? 'Long paths active: NS, EW' : 'Short paths active: NE, NW, SE, SW')}
                    </div>
                </div>
                <div style="font-size: 9px; color: #666; margin-top: 4px;">Right-click to toggle switch</div>
            </div>
            <div class="property-item" style="background: #2a2a3e; padding: 8px; border-radius: 4px; margin: 8px 0;">
                <label style="color: #00d4ff; font-weight: bold;">Switch Points:</label>
                <pre style="font-size: 10px; margin: 4px 0 0 0; color: #ccc;">${
                    Object.entries(track.switch_points).map(([entry, exits]) => 
                        `${entry} -> [${exits.join(', ')}]`
                    ).join('\n')
                }</pre>
            </div>
            ` : ''}
            ${track.type === 'signal' ? `
            <div class="property-item">
                <label>Signal State:</label>
                <span style="color: ${track.signal_state === 'GREEN' ? '#04d361' : '#e83f5b'}">${track.signal_state}</span>
            </div>
            <div class="property-item">
                <label>Direction:</label>
                <span>${track.signal_direction}</span>
            </div>
            ` : ''}
            ${track.block_section_id ? `
            <div class="property-item">
                <label>Block ID:</label>
                <span>${track.block_section_id}</span>
            </div>
            ` : ''}
            ${track.occupied_by ? `
            <div class="property-item">
                <label>Occupied By:</label>
                <span style="color: #e83f5b">${track.occupied_by}</span>
            </div>
            ` : ''}
            ${track.dead_end_ports && track.dead_end_ports.length > 0 ? `
            <div class="property-item">
                <label>Dead Ends:</label>
                <span>${track.dead_end_ports.join(', ')}</span>
            </div>
            ` : ''}
            
            <!-- Raw Properties (Collapsible) -->
            <details class="raw-properties" style="margin-top: 12px; background: #1a1a1e; border-radius: 4px; padding: 8px;">
                <summary style="cursor: pointer; color: #888; font-size: 11px; user-select: none;">
                    ▸ All Properties (JSON)
                </summary>
                <pre style="font-size: 10px; color: #aaa; margin: 8px 0 0 0; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; background: #0a0a0e; padding: 8px; border-radius: 4px;">${JSON.stringify(track, null, 2)}</pre>
            </details>
        `;
        
        // Initialize track bits editor after panel is rendered
        setTimeout(() => this.initBitEditor(track), 0);
        setTimeout(() => this.initVisualTrackEditor(editorId, track), 0);
    }

    initBitEditor(track) {
        const bitEditor = document.getElementById('bit-editor');
        if (!bitEditor) return;

        const calculateBitsFromCheckboxes = () => {
            let bits = 0;
            document.querySelectorAll('#bit-editor input[type="checkbox"]').forEach(cb => {
                if (cb.checked) {
                    bits |= parseInt(cb.dataset.bit);
                }
            });
            return bits;
        };

        const updateBitDisplay = () => {
            const bits = calculateBitsFromCheckboxes();
            const totalSpan = document.getElementById('bit-total');
            const binarySpan = document.getElementById('bit-binary');
            if (totalSpan) totalSpan.textContent = bits;
            if (binarySpan) binarySpan.textContent = bits.toString(2).padStart(6, '0');

            // Update checkbox styling
            document.querySelectorAll('#bit-editor input[type="checkbox"]').forEach(cb => {
                const label = cb.parentElement;
                const span = label.querySelector('span');
                const isChecked = cb.checked;
                label.style.background = isChecked ? '#2a4a3e' : '#2a2a34';
                label.style.borderColor = isChecked ? '#00ff88' : 'transparent';
                if (span) span.style.color = isChecked ? '#00ff88' : '#ccc';
            });

            // Show/hide warning if forbidden
            const warning = document.querySelector('.bit-warning');
            if (warning) {
                warning.style.display = FORBIDDEN_TRACK_BITS.has(bits) ? 'block' : 'none';
            }
        };

        // Add change listeners to all checkboxes
        const checkboxes = bitEditor.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.addEventListener('change', () => updateBitDisplay());
        });

        // Apply button handler
        const applyButton = document.getElementById('apply-bits');
        if (applyButton) {
            applyButton.onclick = async () => {
                const newBits = calculateBitsFromCheckboxes();

                // Validate
                if (FORBIDDEN_TRACK_BITS.has(newBits)) {
                    alert('Cannot apply forbidden configuration');
                    return;
                }

                try {
                    if (track.isVirtual) {
                        // Create new track via API for virtual tiles
                        const newTrack = {
                            x: track.x,
                            y: track.y,
                            track_bits: newBits,
                            directed_paths: []
                        };
                        const result = await window.api.addTracksBulk([newTrack]);
                        console.log('New track created:', result);
                    } else {
                        // Update existing track via API
                        const result = await window.api.updateTrack(track.x, track.y, { track_bits: newBits });
                        console.log('Track bits updated:', result);
                    }

                    // Refresh network and properties panel
                    await window.app.refreshNetwork();
                    await window.editor.selectElement({x: track.x, y: track.y});
                } catch (err) {
                    console.error('Failed to update track bits:', err);
                    alert('Failed to update track bits: ' + err.message);
                }
            };
        }
    }
    
    // Visual editor for track segments (6-bit system)
    // Uses isometric diamond layout matching the main renderer
    initVisualTrackEditor(canvasId, track) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        
        const ctx = canvas.getContext('2d');
        // Make canvas larger for better visibility
        canvas.width = 200;
        canvas.height = 200;
        const size = 200;
        const half = size / 2;
        const quarter = size / 4;
        
        // Segment definitions: bit value -> [edge1, edge2, label]
        const SEGMENTS = {
            1:  { edges: ['N', 'S'], label: 'NS', color: '#00ff88' },
            2:  { edges: ['E', 'W'], label: 'EW', color: '#00ff88' },
            4:  { edges: ['N', 'E'], label: 'NE', color: '#00d4ff' },
            8:  { edges: ['N', 'W'], label: 'NW', color: '#00d4ff' },
            16: { edges: ['S', 'E'], label: 'SE', color: '#00d4ff' },
            32: { edges: ['S', 'W'], label: 'SW', color: '#00d4ff' },
        };
        
        // Edge midpoints on isometric diamond canvas
        // Diamond corners: top(half,10), right(size-10,half), bottom(half,size-10), left(10,half)
        // Updated mapping to match renderer projection (Y+ points upper-right):
        //   N = top-right edge midpoint (between top and right corners)
        //   E = bottom-right edge midpoint (between right and bottom corners)
        //   S = bottom-left edge midpoint (between left and bottom corners)
        //   W = top-left edge midpoint (between top and left corners)
        const edgePositions = {
            'N': { x: half + quarter - 5, y: half - quarter + 5 },   // top-right edge (Y+)
            'E': { x: half + quarter - 5, y: half + quarter - 5 },   // bottom-right edge (X+)
            'S': { x: half - quarter + 5, y: half + quarter - 5 },   // bottom-left edge (Y-)
            'W': { x: half - quarter + 5, y: half - quarter + 5 }    // top-left edge (X-)
        };
        
        // Current track_bits state
        let currentBits = track.track_bits ?? 0;
        
        // Draw the editor
        const drawEditor = () => {
            ctx.clearRect(0, 0, size, size);
            
            // Background
            ctx.fillStyle = '#111';
            ctx.fillRect(0, 0, size, size);
            
            // Draw diamond outline
            ctx.strokeStyle = '#333';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(half, 10);         // top
            ctx.lineTo(size - 10, half);  // right
            ctx.lineTo(half, size - 10);  // bottom
            ctx.lineTo(10, half);         // left
            ctx.closePath();
            ctx.stroke();
            
            // Draw all possible segments as faint dashed lines (guides)
            ctx.setLineDash([2, 4]);
            ctx.strokeStyle = '#222';
            ctx.lineWidth = 1;
            for (const [bit, seg] of Object.entries(SEGMENTS)) {
                const [e1, e2] = seg.edges;
                const start = edgePositions[e1];
                const end = edgePositions[e2];
                ctx.beginPath();
                ctx.moveTo(start.x, start.y);
                ctx.lineTo(end.x, end.y);
                ctx.stroke();
            }
            ctx.setLineDash([]);
            
            // Draw active segments
            for (const [bit, seg] of Object.entries(SEGMENTS)) {
                if (currentBits & parseInt(bit)) {
                    const [e1, e2] = seg.edges;
                    const start = edgePositions[e1];
                    const end = edgePositions[e2];
                    
                    ctx.strokeStyle = seg.color;
                    ctx.lineWidth = 3;
                    ctx.beginPath();
                    ctx.moveTo(start.x, start.y);
                    ctx.lineTo(end.x, end.y);
                    ctx.stroke();
                    
                    // Draw segment label at midpoint with better visibility
                    const midX = (start.x + end.x) / 2;
                    const midY = (start.y + end.y) / 2;
                    ctx.font = 'bold 12px sans-serif';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    // Draw text with dark outline for visibility
                    ctx.strokeStyle = '#000';
                    ctx.lineWidth = 3;
                    ctx.strokeText(seg.label, midX, midY);
                    ctx.fillStyle = '#fff';
                    ctx.fillText(seg.label, midX, midY);
                }
            }
            
            // Draw edge indicators
            for (const [name, pos] of Object.entries(edgePositions)) {
                // Check if this edge is connected by any active segment
                let isConnected = false;
                for (const [bit, seg] of Object.entries(SEGMENTS)) {
                    if ((currentBits & parseInt(bit)) && seg.edges.includes(name)) {
                        isConnected = true;
                        break;
                    }
                }
                
                ctx.fillStyle = isConnected ? '#00ff88' : '#444';
                ctx.strokeStyle = isConnected ? '#fff' : '#666';
                ctx.lineWidth = isConnected ? 3 : 2;
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
                ctx.fill();
                ctx.stroke();
                
                // Label with better visibility
                ctx.fillStyle = isConnected ? '#000' : '#ccc';
                ctx.font = 'bold 10px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(name, pos.x, pos.y);
            }
            
            // Center indicator for forbidden or complex
            if (FORBIDDEN_TRACK_BITS.has(currentBits)) {
                ctx.fillStyle = '#e83f5b';
                ctx.beginPath();
                ctx.arc(half, half, 8, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#fff';
                ctx.font = 'bold 10px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('!', half, half);
            }
            
            // Status text with better visibility
            const numSegments = Object.keys(SEGMENTS).filter(bit => currentBits & parseInt(bit)).length;
            ctx.fillStyle = FORBIDDEN_TRACK_BITS.has(currentBits) ? '#e83f5b' : '#aaa';
            ctx.font = 'bold 12px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(`${numSegments} segment${numSegments !== 1 ? 's' : ''} (bits: ${currentBits})`, half, size - 10);
        };
        
        // Save changes to server
        const saveToServer = async () => {
            if (FORBIDDEN_TRACK_BITS.has(currentBits)) {
                console.warn('Cannot save forbidden configuration');
                return;
            }
            
            try {
                await this.api.updateTrack(track.x, track.y, { track_bits: currentBits });
                await window.app.refreshNetwork();
                await window.editor.selectElement({x: track.x, y: track.y});
            } catch (err) {
                console.error('Failed to update track bits:', err);
            }
        };
        
        // Initial draw
        drawEditor();
        
        // Click handler: toggle segment
        canvas.addEventListener('click', async (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            
            // Find closest segment to click point
            let closestBit = null;
            let minDist = 12;
            
            for (const [bit, seg] of Object.entries(SEGMENTS)) {
                const [e1, e2] = seg.edges;
                const start = edgePositions[e1];
                const end = edgePositions[e2];
                const dist = this.pointToSegmentDistance(x, y, start.x, start.y, end.x, end.y);
                if (dist < minDist) {
                    minDist = dist;
                    closestBit = parseInt(bit);
                }
            }
            
            if (closestBit !== null) {
                // Toggle the segment
                currentBits ^= closestBit;
                
                // Update checkboxes in bit editor
                const checkbox = document.querySelector(`#bit-editor input[data-bit="${closestBit}"]`);
                if (checkbox) {
                    checkbox.checked = !!(currentBits & closestBit);
                    // Trigger change event to update display
                    checkbox.dispatchEvent(new Event('change'));
                }
                
                drawEditor();
                await saveToServer();
            }
        });
    }
    
    pointToSegmentDistance(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        const lenSq = dx * dx + dy * dy;
        
        if (lenSq === 0) return Math.sqrt((px - x1) * (px - x1) + (py - y1) * (py - y1));
        
        let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
        t = Math.max(0, Math.min(1, t));
        
        const nearX = x1 + t * dx;
        const nearY = y1 + t * dy;
        
        return Math.sqrt((px - nearX) * (px - nearX) + (py - nearY) * (py - nearY));
    }

    clearPropertiesPanel() {
        const panel = document.querySelector('.properties-panel');
        if (panel) {
            panel.innerHTML = `
                <h3>Properties</h3>
                <p style="color: #666; font-size: 12px;">Select an element to view properties</p>
            `;
        }
    }

    setCachedNetwork(network) {
        this.cachedNetwork = network;
    }
}
