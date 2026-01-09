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

        // Check destination validity for signals and stations
        const endTrack = trackMap.get(`${endX},${endY}`);
        if (endTrack) {
            if (endTrack.type === 'station' || endTrack.station_id) {
                // Stations: only allow connecting to endpoint tiles (first or last position)
                // Middle tiles of a platform cannot be connection targets
                const stationId = endTrack.station_id;
                if (stationId && existingTracks) {
                    const stationTiles = existingTracks.filter(t => t.station_id === stationId);
                    if (stationTiles.length > 1) {
                        // Find endpoints by checking which tiles have fewer station neighbors
                        const isEndpoint = stationTiles.length <= 2 || this.isStationEndpoint(endX, endY, stationTiles);
                        if (!isEndpoint) {
                            return null; // Cannot connect to middle of platform
                        }
                    }
                }
            }
            // Signals: allowed as destinations - they preserve underlying track connectivity
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

                const existingTrack = trackMap.get(nKey);
                
                // For signals and stations: only allow if we can use their existing track ports
                // We enter from the opposite direction of our movement
                const entryDir = this.getOppositeDir(neighbor.dir);
                
                if (existingTrack && (existingTrack.type === 'signal' || existingTrack.signal_state)) {
                    // Signal tile: can only traverse if entering through an existing port
                    const signalPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
                    if (!signalPorts.includes(entryDir)) {
                        continue; // Can't enter signal from this direction
                    }
                }
                
                if (existingTrack && (existingTrack.type === 'station' || existingTrack.station_id)) {
                    // Station tile: can only traverse if entering through an existing port
                    const stationPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
                    if (!stationPorts.includes(entryDir)) {
                        continue; // Can't enter station from this direction
                    }
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

    // Check if a position is an endpoint of a station (first or last tile)
    // Endpoints have only one adjacent station tile, middle tiles have two
    isStationEndpoint(x, y, stationTiles) {
        let adjacentStationCount = 0;
        const deltas = [
            { dx: 0, dy: 1 },
            { dx: 0, dy: -1 },
            { dx: 1, dy: 0 },
            { dx: -1, dy: 0 }
        ];
        
        for (const d of deltas) {
            const hasAdjacentStation = stationTiles.some(t => 
                t.x === x + d.dx && t.y === y + d.dy
            );
            if (hasAdjacentStation) adjacentStationCount++;
        }
        
        // Endpoint has 0 or 1 adjacent station tiles (not 2 which would be middle)
        return adjacentStationCount <= 1;
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

        // Protection: Don't touch station/platform tiles or signal tiles
        // These already have track and we should not modify them
        const existingForProtection = existingTracks.get(`${current.x},${current.y}`);
        if (existingForProtection && (existingForProtection.type === 'station' || existingForProtection.station_id)) {
            continue;
        }
        if (existingForProtection && (existingForProtection.type === 'signal' || existingForProtection.signal_state)) {
            continue; // Don't modify signal tiles - use their existing track
        }

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

            let foundConnection = false;

            // Check existing track on CURRENT tile first
            const existingTrack = existingTracks.get(`${current.x},${current.y}`);
            if (existingTrack && existingTrack.track_bits) {
                // Helper to find connected port on existing track
                const getConnectedPort = (bits, port) => {
                    // NS (1)
                    if ((bits & 1) && (port === 'NORTH' || port === 'SOUTH')) return port === 'NORTH' ? 'SOUTH' : 'NORTH';
                    // EW (2)
                    if ((bits & 2) && (port === 'EAST' || port === 'WEST')) return port === 'EAST' ? 'WEST' : 'EAST';
                    // NE (4)
                    if ((bits & 4) && (port === 'NORTH' || port === 'EAST')) return port === 'NORTH' ? 'EAST' : 'NORTH';
                    // NW (8)
                    if ((bits & 8) && (port === 'NORTH' || port === 'WEST')) return port === 'NORTH' ? 'WEST' : 'NORTH';
                    // SE (16)
                    if ((bits & 16) && (port === 'SOUTH' || port === 'EAST')) return port === 'SOUTH' ? 'EAST' : 'SOUTH';
                    // SW (32)
                    if ((bits & 32) && (port === 'SOUTH' || port === 'WEST')) return port === 'SOUTH' ? 'WEST' : 'SOUTH';
                    return null;
                };

                const connectedPort = getConnectedPort(existingTrack.track_bits, exitDir);
                if (connectedPort) {
                    addPath(current.x, current.y, connectedPort, exitDir, 'both');
                    foundConnection = true;
                }
            }

            if (!foundConnection) {
                // Check all 4 neighbors for existing connections
                const neighbors = [
                    { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
                    { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
                    { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
                    { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' }
                ];

                for (const n of neighbors) {
                    if (n.myPort === exitDir) continue; // Skip direction we're going

                    if (neighborHasPortTo(current.x + n.dx, current.y + n.dy, n.theirPort)) {
                        addPath(current.x, current.y, n.myPort, exitDir, 'both');
                        foundConnection = true;
                    }
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
                window.showToast(`Cannot create forbidden configuration (${trackBits}). This would require a 3-way switch.`, 'error', 5000);
            }
            continue;  // Skip this track
        }

        // Add track_bits to track data
        track.track_bits = trackBits;
        tracks.push(track);
    }

    return tracks;
}

/**
 * Convert a zigzag path to diagonal/short-path tracks.
 * 
 * Instead of creating alternating NS and EW tracks (straight zigzag),
 * this creates alternating diagonal tracks (NW/SE or NE/SW pattern).
 * 
 * The diagonal direction is determined by examining each step:
 * - For proper connectivity, we look at how to connect each tile
 * - NW connects North edge to West edge
 * - SE connects South edge to East edge
 * - NE connects North edge to East edge  
 * - SW connects South edge to West edge
 * 
 * For a diagonal siding going SE (x increasing, y decreasing):
 *   NW at (9,0) -> SE at (9,1) -> NW at (10,1) -> SE at (10,2) etc.
 * 
 * @param {Array} path - Array of {x, y} positions
 * @param {Object} cachedNetwork - Network data with existing tracks
 * @returns {Array} - Array of track objects with track_bits for diagonal tracks
 */
function pathToDiagonalTracks(path, cachedNetwork) {
    if (!path || path.length < 2) return [];
    
    const tracks = [];
    const existingTracks = new Map();
    
    // Build map of existing tracks
    if (cachedNetwork && cachedNetwork.tracks) {
        for (const track of cachedNetwork.tracks) {
            existingTracks.set(`${track.x},${track.y}`, track);
        }
    }
    
    // Determine overall direction of the path
    const start = path[0];
    const end = path[path.length - 1];
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    
    // For diagonal sidings, we need two diagonal types that connect to each other
    // The pattern depends on the general direction:
    //
    // Going SE (dx > 0, dy < 0) or NW (dx < 0, dy > 0): Use NW/SE pair
    //   NW connects N-W edges, SE connects S-E edges
    //   Pattern: NW -> SE -> NW -> SE (stair-stepping SE or NW)
    //
    // Going NE (dx > 0, dy > 0) or SW (dx < 0, dy < 0): Use NE/SW pair
    //   NE connects N-E edges, SW connects S-W edges
    //   Pattern: NE -> SW -> NE -> SW (stair-stepping NE or SW)
    
    let diagonalPair;
    if ((dx >= 0 && dy <= 0) || (dx <= 0 && dy >= 0)) {
        // Going SE or NW -> use NW/SE pair
        diagonalPair = ['NW', 'SE'];
    } else {
        // Going NE or SW -> use NE/SW pair
        diagonalPair = ['NE', 'SW'];
    }

    // Track bit mapping for diagonal paths
    const diagonalBits = {
        'NE': 4,   // TRACK_NE
        'NW': 8,   // TRACK_NW
        'SE': 16,  // TRACK_SE
        'SW': 32   // TRACK_SW
    };
    
    // Port mapping for diagonal paths
    const diagonalPorts = {
        'NE': { port1: 'NORTH', port2: 'EAST' },
        'NW': { port1: 'NORTH', port2: 'WEST' },
        'SE': { port1: 'SOUTH', port2: 'EAST' },
        'SW': { port1: 'SOUTH', port2: 'WEST' }
    };
    
    // Process each tile in the path
    for (let i = 0; i < path.length; i++) {
        const pos = path[i];
        const key = `${pos.x},${pos.y}`;
        const existing = existingTracks.get(key);

        // Protection: Don't touch station/platform tiles
        if (existing && (existing.type === 'station' || existing.station_id)) {
            continue;
        }

        // Alternate between the two diagonals in the pair
        const diagonalType = diagonalPair[i % 2];
        const ports = diagonalPorts[diagonalType];
        let trackBits = diagonalBits[diagonalType];
        
        // If there's an existing track, merge with it (create a switch)
        if (existing && existing.track_bits) {
            trackBits = existing.track_bits | trackBits;
        }
        
        // Check for forbidden configurations
        if (FORBIDDEN_TRACK_BITS.has(trackBits)) {
            console.warn(`[pathToDiagonalTracks] Skipping forbidden configuration (${trackBits}) at (${pos.x}, ${pos.y})`);
            continue;
        }
        
        tracks.push({
            x: pos.x,
            y: pos.y,
            track_bits: trackBits,
            directed_paths: [{ port1: ports.port1, port2: ports.port2, direction: 'both' }]
        });
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

// ============================================================================
// Signal Placement Validation (based on Reference_Signals_and_Blocks.md)
// ============================================================================

// Valid detection directions for each track_bits configuration
// Detection direction = where trains travel TO (the direction behind the signal)
// Signals can ONLY be placed on simple tracks (non-switch tiles)
const VALID_DETECTION_DIRECTIONS = {
    // Only simple single-segment tracks allow signals
    // Crossings (3, 24, 36) are banned for signal placement
    1:  ['NORTH', 'SOUTH'],      // NS straight - trains can go north or south
    2:  ['EAST', 'WEST'],        // EW straight - trains can go east or west
    4:  ['NORTH', 'EAST'],       // NE curve - trains can go north or east
    8:  ['NORTH', 'WEST'],       // NW curve - trains can go north or west
    16: ['SOUTH', 'EAST'],       // SE curve - trains can go south or east
    32: ['SOUTH', 'WEST'],       // SW curve - trains can go south or west
};

// Configurations where signals are NOT allowed
const SWITCH_TRACK_BITS = new Set([
    // Crossings (banned for signals)
    3, 24, 36,
    // Y-switches
    5, 9, 17, 33,   // NS + diagonal
    6, 10, 18, 34,  // EW + diagonal
    // Diagonal switches
    12, 20, 40, 48,
    // Double slip crossings (3+ segments)
    7, 11, 19, 35, 14, 38, 26, 50, 21, 37, 25, 41
]);

// Get valid detection directions for a track_bits value
function getValidSignalDirections(trackBits) {
    if (SWITCH_TRACK_BITS.has(trackBits)) {
        return []; // No signals on switches
    }
    return VALID_DETECTION_DIRECTIONS[trackBits] || [];
}

// Check if a signal can be placed on a track with given track_bits
function canPlaceSignal(trackBits) {
    return getValidSignalDirections(trackBits).length > 0;
}

// Get the recommended default signal direction for a track_bits
// Returns the first valid direction
function getDefaultSignalDirection(trackBits) {
    const valid = getValidSignalDirections(trackBits);
    return valid.length > 0 ? valid[0] : null;
}

// Convert short direction (N/E/S/W) to full name (NORTH/EAST/SOUTH/WEST)
function directionToFull(dir) {
    const map = { 'N': 'NORTH', 'E': 'EAST', 'S': 'SOUTH', 'W': 'WEST' };
    return map[dir] || dir;
}

// Convert full direction name to short form
function directionToShort(dir) {
    if (!dir) return null;
    return dir.charAt(0).toUpperCase();
}

// ============================================================================
// Block Assignment Validation (based on Reference_Signals_and_Blocks.md)
// ============================================================================

// Check if track_bits represents a crossing (+ shape or X shape)
// Crossings are isolated paths that don't connect - blocks cannot span them
function isCrossingForBlock(trackBits) {
    // NS (1) + EW (2) = 3 is a standard + crossing
    // NE (4) + SW (32) = 36 is a diagonal X crossing
    // NW (8) + SE (16) = 24 is a diagonal X crossing
    return trackBits === 3 || trackBits === 24 || trackBits === 36;
}

// Check if a track tile is valid for block assignment
// Returns { valid: boolean, reason: string }
// Note: Stations are now allowed in blocks, but blocks cannot cross station boundaries
function validateBlockTile(track) {
    if (!track) {
        return { valid: false, reason: 'No track at this position' };
    }

    // Rule 1: Blocks MUST NOT include switches
    if (track.is_switch) {
        return { valid: false, reason: 'Switches cannot be inside a block' };
    }

    // Rule 2: Blocks MUST NOT include crossings (+ or X shape)
    // Crossings have isolated paths that don't connect, so blocks cannot span them
    if (isCrossingForBlock(track.track_bits)) {
        return { valid: false, reason: 'Crossings cannot be inside a block - paths are isolated' };
    }

    // Note: Signals create block BOUNDARIES, so a tile with a signal
    // marks the end of one block and start of another.
    // We allow signals in the path but warn the user that it creates a boundary.

    return { valid: true, reason: null };
}

// Validate a path for block assignment
// mode: 'create' (new block) or 'extend' (extend existing block)
// Returns { valid: boolean, warnings: string[], errors: string[], blocksInPath: string[], signalsInPath: object[] }
function validateBlockPath(path, tracks, mode = 'create') {
    const warnings = [];
    const errors = [];
    const blocksInPath = new Set();
    const signalsInPath = [];
    const trackMap = new Map();

    // Build track lookup map
    if (tracks) {
        tracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));
    }

    let hasSignal = false;
    for (const pos of path) {
        const track = trackMap.get(`${pos.x},${pos.y}`);
        const validation = validateBlockTile(track);

        if (!validation.valid) {
            errors.push(`(${pos.x},${pos.y}): ${validation.reason}`);
            continue;
        }

        // Track existing block assignments
        if (track && track.block_section_id) {
            blocksInPath.add(track.block_section_id);
        }

        // Check for signals - they create boundaries
        if (track && (track.signal_state || track.type === 'signal')) {
            hasSignal = true;
            signalsInPath.push({ x: pos.x, y: pos.y, track });
        }
    }

    // Mode-specific validation
    if (mode === 'create') {
        // For 'create' mode: ALL tracks must have NO block assigned
        if (blocksInPath.size > 0) {
            errors.push(`Path contains tracks already assigned to block(s): ${[...blocksInPath].join(', ')}`);
        }
    } else if (mode === 'extend') {
        // For 'extend' mode: Must start on a track WITH a block
        const startTrack = trackMap.get(`${path[0].x},${path[0].y}`);
        if (!startTrack || !startTrack.block_section_id) {
            errors.push('Extend mode must start on a track that already has a block assigned');
        }
    }

    if (hasSignal && errors.length === 0) {
        if (mode === 'extend') {
            warnings.push('Path contains signal(s) - they will need reconfiguration after extending');
        } else {
            warnings.push('Path contains signal(s) - signals create block boundaries');
        }
    }

    return {
        valid: errors.length === 0,
        warnings,
        errors,
        blocksInPath: [...blocksInPath],
        signalsInPath
    };
}

// Pathfinder that ONLY follows existing tracks (no new tracks)
// Used for block assignment and eraser tools
class ExistingTrackPathfinder {
    constructor() {
        this.pathfinder = new ConstructionPathfinder();
    }

    findPathOnExistingTracks(startX, startY, endX, endY, existingTracks) {
        const trackMap = new Map();
        existingTracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));

        // Must start and end on existing track
        if (!trackMap.has(`${startX},${startY}`)) return null;
        if (!trackMap.has(`${endX},${endY}`)) return null;

        // BFS along existing track connections only
        const start = { x: startX, y: startY };
        const end = { x: endX, y: endY };
        const queue = [{ pos: start, path: [start] }];
        const visited = new Set();

        while (queue.length > 0) {
            const { pos, path } = queue.shift();
            const key = `${pos.x},${pos.y}`;

            if (pos.x === end.x && pos.y === end.y) {
                return path;
            }

            if (visited.has(key)) continue;
            visited.add(key);

            const track = trackMap.get(key);
            if (!track) continue;

            // Get connected edges from track_bits
            const trackBits = track.track_bits || 0;
            const connectedEdges = this.pathfinder.getExistingTrackDirections(trackBits);

            // Explore neighbors that are connected by track
            const neighbors = [
                { x: pos.x, y: pos.y + 1, dir: 'NORTH', entry: 'SOUTH' },
                { x: pos.x + 1, y: pos.y, dir: 'EAST', entry: 'WEST' },
                { x: pos.x, y: pos.y - 1, dir: 'SOUTH', entry: 'NORTH' },
                { x: pos.x - 1, y: pos.y, dir: 'WEST', entry: 'EAST' }
            ];

            for (const n of neighbors) {
                // Check if current tile has path to this neighbor
                if (!connectedEdges.includes(n.dir)) continue;

                // Check if neighbor exists and has path connecting back
                const neighborKey = `${n.x},${n.y}`;
                if (visited.has(neighborKey)) continue;

                const neighborTrack = trackMap.get(neighborKey);
                if (!neighborTrack) continue;

                const neighborEdges = this.pathfinder.getExistingTrackDirections(neighborTrack.track_bits || 0);
                if (!neighborEdges.includes(n.entry)) continue;

                queue.push({
                    pos: { x: n.x, y: n.y },
                    path: [...path, { x: n.x, y: n.y }]
                });
            }
        }

        return null; // No path found
    }
}

class Editor {
    constructor(renderer, api) {
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
        this.cachedBlocks = [];  // Block Manager cache
        this.selectedBlockId = null;  // Currently selected block in Block Manager

        // Hover preview for signal/station tools
        this.hoverPosition = null;
        this.hoverValid = false;
        this.previewSignalDirection = null;  // Current preview direction for signal tool (cycles on right-click)
        this.previewSignalDirectionIndex = 0;  // Index into valid directions array
        
        // Diagonal track building mode (shift key)
        this.shiftKeyHeld = false;
        this.diagonalBuildMode = false;  // True when building diagonal/short-path tracks

        // Station extend mode
        this.stationExtendMode = { active: false };
        
        // Block extend mode
        this.blockExtendMode = { active: false };

        // Route tool state
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,           // {x, y} of clicked tile
            reachableEndpoints: [],        // Block IDs from API
            reachablePositions: new Set(), // Tile positions for quick lookup
            priority: 5,                   // Default priority (1-10)
            trainId: null                  // Auto-detected or user-selected
        };
        this.cachedPaths = [];            // List of active routes

        this.pathfinder = new ConstructionPathfinder();
        this.existingOnlyPathfinder = new ExistingTrackPathfinder();
        this.setupEventListeners();
        this.setupBlockManager();
        this.setupSecondarySidebar();
    }

    // === Secondary Sidebar Management ===

    // Track currently open editor for auto-refresh
    _currentEditorType = null;  // 'block', 'signal', or 'station'
    _currentEditorId = null;    // The ID of the item being edited

    setupSecondarySidebar() {
        const closeBtn = document.getElementById('close-secondary-sidebar');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => this.hideSecondarySidebar());
        }
    }

    // === Collapsible Tool Categories ===
    
    setupCollapsibleCategories() {
        const categories = document.querySelectorAll('.tool-category');
        
        categories.forEach(category => {
            const label = category.querySelector('.tool-category-label');
            if (!label) return;
            
            // Make label clickable to toggle collapse
            label.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleCategory(category);
            });
        });
        
        // Load saved collapse states from localStorage
        this.loadCategoryStates();
        
        // Ensure the category containing the currently selected tool is expanded
        this.expandCategoryForTool(this.selectedTool);
    }
    
    toggleCategory(category) {
        const wasCollapsed = category.classList.contains('collapsed');
        
        if (wasCollapsed) {
            // Expand this category
            category.classList.remove('collapsed');
        } else {
            // Collapse this category
            category.classList.add('collapsed');
        }
        
        // Save state to localStorage
        this.saveCategoryStates();
    }
    
    expandCategoryForTool(toolName) {
        if (!toolName) return;
        
        // Find the category containing this tool
        const toolBtn = document.querySelector(`.tool-btn[data-tool="${toolName}"]`);
        if (!toolBtn) return;
        
        const category = toolBtn.closest('.tool-category');
        if (category && category.classList.contains('collapsed')) {
            category.classList.remove('collapsed');
            this.saveCategoryStates();
        }
    }
    
    saveCategoryStates() {
        const categories = document.querySelectorAll('.tool-category');
        const states = {};
        
        categories.forEach((category, index) => {
            const label = category.querySelector('.tool-category-label');
            const categoryName = label ? label.textContent.trim() : `category-${index}`;
            states[categoryName] = category.classList.contains('collapsed');
        });
        
        try {
            localStorage.setItem('toolCategoryStates', JSON.stringify(states));
        } catch (e) {
            // localStorage not available, silently ignore
        }
    }
    
    loadCategoryStates() {
        try {
            const saved = localStorage.getItem('toolCategoryStates');
            if (!saved) return;
            
            const states = JSON.parse(saved);
            const categories = document.querySelectorAll('.tool-category');
            
            categories.forEach((category, index) => {
                const label = category.querySelector('.tool-category-label');
                const categoryName = label ? label.textContent.trim() : `category-${index}`;
                
                if (states[categoryName] === true) {
                    category.classList.add('collapsed');
                }
            });
        } catch (e) {
            // localStorage not available or invalid data, silently ignore
        }
    }

    showSecondarySidebar(title, content) {
        const sidebar = document.getElementById('secondary-sidebar');
        const titleEl = document.getElementById('secondary-sidebar-title');
        const contentEl = document.getElementById('secondary-sidebar-content');

        if (sidebar && titleEl && contentEl) {
            titleEl.textContent = title;
            contentEl.innerHTML = content;
            
            // Remove compact class by default (specific panels can add it after)
            sidebar.classList.remove('compact');
            
            // Position next to main sidebar
            const mainSidebar = document.querySelector('.sidebar');
            if (mainSidebar) {
                sidebar.style.left = mainSidebar.offsetWidth + 'px';
            }
            
            sidebar.classList.add('visible');
        }
    }

    hideSecondarySidebar() {
        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.remove('visible');
            sidebar.classList.remove('compact');
        }
        // Clear editor tracking
        this._currentEditorType = null;
        this._currentEditorId = null;
    }

    // Show full JSON viewer in secondary sidebar
    showFullJSONViewer(data, title = 'JSON Viewer') {
        const jsonString = JSON.stringify(data, null, 2);
        
        const content = `
            <div class="json-viewer-panel" style="background: transparent; height: 100%; display: flex; flex-direction: column;">
                <!-- Copy Button -->
                <div style="margin-bottom: 12px;">
                    <button id="copy-json-btn" class="btn btn-primary btn-full text-sm">
                        📋 Copy to Clipboard
                    </button>
                </div>

                <!-- JSON Content (scrollable) -->
                <div style="flex: 1; overflow: auto; background: #0d1020; border: 1px solid #3a3a4e; border-radius: 6px; padding: 12px;">
                    <pre id="json-content" style="margin: 0; font-family: 'Courier New', monospace; font-size: 11px; color: #00ff88; line-height: 1.4; white-space: pre-wrap; word-wrap: break-word;">${this.syntaxHighlightJSON(jsonString)}</pre>
                </div>
            </div>
        `;

        this.showSecondarySidebar(title, content);

        // Initialize copy button handler
        setTimeout(() => {
            const copyBtn = document.getElementById('copy-json-btn');
            if (copyBtn) {
                copyBtn.onclick = () => {
                    navigator.clipboard.writeText(jsonString).then(() => {
                        window.showToast('JSON copied to clipboard', 'success', 2000);
                        copyBtn.textContent = '✓ Copied!';
                        copyBtn.classList.remove('btn-primary');
                        copyBtn.classList.add('btn-success');
                        setTimeout(() => {
                            copyBtn.textContent = '📋 Copy to Clipboard';
                            copyBtn.classList.remove('btn-success');
                            copyBtn.classList.add('btn-primary');
                        }, 2000);
                    }).catch(err => {
                        console.error('Failed to copy JSON:', err);
                        window.showToast('Failed to copy JSON', 'error', 3000);
                    });
                };
            }
        }, 0);
    }

    // Syntax highlight JSON string for display
    syntaxHighlightJSON(jsonString) {
        return jsonString
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, (match) => {
                let cls = 'json-number';
                if (/^"/.test(match)) {
                    if (/:$/.test(match)) {
                        cls = 'json-key';
                    } else {
                        cls = 'json-string';
                    }
                } else if (/true|false/.test(match)) {
                    cls = 'json-boolean';
                } else if (/null/.test(match)) {
                    cls = 'json-null';
                }
                
                const colors = {
                    'json-key': '#00d4ff',
                    'json-string': '#ffd93d',
                    'json-number': '#ff79c6',
                    'json-boolean': '#04d361',
                    'json-null': '#888'
                };
                
                return `<span style="color: ${colors[cls]};">${match}</span>`;
            });
    }

    /**
     * Update the secondary editor panel when a new tile is selected.
     * If the editor is open and the new tile has relevant content, reload.
     * If the new tile doesn't have relevant content, close the editor.
     * @param {Object} track - The track data for the newly selected tile
     */
    updateSecondaryEditorForTile(track) {
        // If no editor is open, nothing to do
        if (!this._currentEditorType) {
            return;
        }

        switch (this._currentEditorType) {
            case 'signal':
                // Check if the new tile has signals
                if (track.signals && track.signals.length > 0) {
                    // Show signal editor for the signals on this tile
                    this.showSignalsEditorForTile(track);
                } else {
                    // No signals on this tile, close the editor
                    this.hideSecondarySidebar();
                }
                break;

            case 'block':
                // Check if the new tile belongs to a block
                if (track.block_section_id) {
                    // Show block editor for this block
                    this.showBlockEditor(track.block_section_id);
                } else {
                    // No block on this tile, close the editor
                    this.hideSecondarySidebar();
                }
                break;

            case 'station':
                // Check if the new tile has a station
                if (track.station_id) {
                    // Show station editor for this station
                    this.showStationEditor(track.station_id);
                } else {
                    // No station on this tile, close the editor
                    this.hideSecondarySidebar();
                }
                break;
        }
    }

    /**
     * Show signal editor for all signals on a tile.
     * Shows full detail view for each signal.
     * @param {Object} track - The track data containing signals array
     */
    async showSignalsEditorForTile(track) {
        const signals = track.signals || [];

        if (signals.length === 0) {
            this.hideSecondarySidebar();
            return;
        }

        // Track as signal editor (use first signal ID for tracking)
        const firstSignalId = signals[0].signal_id || signals[0].id;
        this._currentEditorType = 'signal';
        this._currentEditorId = firstSignalId;

        // Fetch full details for all signals and build content
        let signalsContent = '';
        const signalDetails = [];

        for (let i = 0; i < signals.length; i++) {
            const signalId = signals[i].signal_id || signals[i].id;
            const signal = await this.api.getSignal(signalId);
            if (!signal || signal.error) continue;

            signalDetails.push({ signal, index: i });
            const state = (signal.state || signal.signal_state || 'green').toUpperCase();
            const stateColor = state === 'GREEN' ? '#04d361' : (state === 'YELLOW' ? '#ffd93d' : '#e83f5b');

            signalsContent += `
                <div class="signal-properties" style="background: #1e2d23; padding: 16px; border-radius: 8px; margin-bottom: 16px; border-left: 4px solid ${stateColor};">

                    ${renderMultiSignalHeader(i + 1, signals.length, signal.signal_id, stateColor)}
                    ${renderSignalStateDisplay(state, stateColor)}

                    <!-- Signal Name Input -->
                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Signal Name</label>
                        <input type="text" class="signal-name-input" data-signal-id="${signal.signal_id}" value="${signal.name || ''}" placeholder="Enter signal name..."
                               style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                    </div>

                    <!-- Direction Display (Read-only) -->
                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Detection Direction (Train travels →)</label>
                        <div style="display: flex; align-items: center; gap: 8px; background: #0d1a12; padding: 10px 12px; border: 1px solid #3a4a3e; border-radius: 6px;">
                            <span style="font-size: 13px; color: ${signal.detection_direction ? '#fff' : '#f55'}; font-weight: bold;">${signal.detection_direction || '???'}</span>
                            <span style="font-size: 10px; color: #666; margin-left: auto;">(Set at creation)</span>
                        </div>
                    </div>

                    <!-- Block to Monitor Dropdown -->
                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Block to Monitor</label>
                        <select class="signal-block-select" data-signal-id="${signal.signal_id}" style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid ${signal.monitored_block_id ? '#3a4a3e' : '#ff9500'}; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                            <option value="">-- None (Manual Control) --</option>
                            <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                        </select>
                        <div class="block-select-hint" data-signal-id="${signal.signal_id}" style="font-size: 10px; color: #666; margin-top: 6px;">
                            Signal shows RED when monitored block is occupied
                        </div>
                    </div>

                    <!-- Save Button -->
                    <button class="save-signal-btn btn btn-primary btn-full text-base" data-signal-id="${signal.signal_id}">
                        Save Signal Properties
                    </button>
                </div>
            `;
        }

        const title = signals.length > 1 ? `Signals (${signals.length})` : 'Signal Editor';
        this.showSecondarySidebar(title, signalsContent);

        // Populate reachable blocks for each signal
        for (const { signal } of signalDetails) {
            try {
                const result = await this.api.getReachableBlocks(signal.signal_id);
                const reachableBlockIds = result.reachable_blocks || [];

                const reachableBlocks = [];
                for (const blockId of reachableBlockIds) {
                    let block = (this.cachedBlocks || []).find(b => b.block_id === blockId);
                    if (!block) {
                        try {
                            block = await this.api.getBlock(blockId);
                            if (block && !block.error) {
                                this.cachedBlocks = this.cachedBlocks || [];
                                this.cachedBlocks.push(block);
                            }
                        } catch (err) {
                            block = { block_id: blockId, name: null };
                        }
                    }
                    if (block) reachableBlocks.push(block);
                }

                const blockSelect = document.querySelector(`.signal-block-select[data-signal-id="${signal.signal_id}"]`);
                if (blockSelect) {
                    const currentValue = signal.monitored_block_id;
                    blockSelect.innerHTML = `
                        <option value="">-- None (Manual Control) --</option>
                        ${reachableBlocks.map(b => `
                            <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                                ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                            </option>
                        `).join('')}
                    `;

                    const hintDiv = document.querySelector(`.block-select-hint[data-signal-id="${signal.signal_id}"]`);
                    if (hintDiv) {
                        if (reachableBlocks.length === 0) {
                            hintDiv.innerHTML = `<span style="color: #ff6b6b;">No reachable blocks found.</span>`;
                        } else {
                            hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span style="color: #888;">${reachableBlocks.length} reachable block(s)</span>`;
                        }
                    }
                }
            } catch (err) {
                console.warn('Failed to fetch reachable blocks for', signal.signal_id, err);
            }
        }

        // Setup save handlers for each signal
        const saveBtns = document.querySelectorAll('.save-signal-btn');
        saveBtns.forEach(btn => {
            btn.onclick = async () => {
                const signalId = btn.dataset.signalId;
                const nameInput = document.querySelector(`.signal-name-input[data-signal-id="${signalId}"]`);
                const blockSelect = document.querySelector(`.signal-block-select[data-signal-id="${signalId}"]`);

                const updates = {};
                if (nameInput) updates.name = nameInput.value.trim() || null;
                if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

                try {
                    await this.api.updateSignal(signalId, updates);
                    window.showToast('Signal updated successfully', 'success', 5000);
                    await window.app.refreshNetwork();

                    // Refresh main panel if we are still on the same tile
                    if (this.selectedElement) {
                        await this.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save signal properties:', err);
                    window.showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    /**
     * Validate that the currently open editor still refers to a valid item.
     * If the item was deleted, close the editor. Called after network refresh.
     */
    validateCurrentEditor() {
        if (!this._currentEditorType || !this._currentEditorId) {
            return;
        }

        const id = this._currentEditorId;
        let itemExists = false;

        switch (this._currentEditorType) {
            case 'block':
                // blocks is an object keyed by block_id
                itemExists = this.cachedNetwork?.blocks && (id in this.cachedNetwork.blocks);
                break;
            case 'signal':
                // signals is an object keyed by signal_id
                itemExists = this.cachedNetwork?.signals && (id in this.cachedNetwork.signals);
                break;
            case 'station':
                // stations is an array
                itemExists = Array.isArray(this.cachedNetwork?.stations) &&
                             this.cachedNetwork.stations.some(s => s.station_id === id);
                break;
        }

        if (!itemExists) {
            // Item was deleted, close the editor
            this.hideSecondarySidebar();
        }
    }

    /**
     * Refresh the secondary editor if it matches the given type and id.
     * Used after API calls that modify specific items.
     * @param {string} type - 'block', 'signal', or 'station'
     * @param {string} id - The ID of the item that was modified
     */
    refreshEditorIfNeeded(type, id) {
        if (this._currentEditorType !== type || this._currentEditorId !== id) {
            return;
        }

        // Don't refresh if user is actively editing an input field
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }

        switch (type) {
            case 'block':
                this.showBlockEditor(id);
                break;
            case 'signal':
                this.showSignalEditor(id);
                break;
            case 'station':
                this.showStationEditor(id);
                break;
        }
    }

    /**
     * Force refresh the currently open secondary editor.
     * Use sparingly - prefer refreshEditorIfNeeded for targeted updates.
     * Skips refresh if an input has focus to avoid interrupting user input.
     */
    refreshCurrentEditor() {
        if (!this._currentEditorType || !this._currentEditorId) {
            return;
        }

        // Don't refresh if user is actively editing an input field
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }

        switch (this._currentEditorType) {
            case 'block':
                this.showBlockEditor(this._currentEditorId);
                break;
            case 'signal':
                this.showSignalEditor(this._currentEditorId);
                break;
            case 'station':
                this.showStationEditor(this._currentEditorId);
                break;
        }
    }

    /**
     * Refresh the main properties panel if the selected element is at the given position.
     * Used after API calls that modify track data.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     */
    async refreshPropertiesPanelIfNeeded(x, y) {
        if (this.selectedElement && this.selectedElement.x === x && this.selectedElement.y === y) {
            await this.selectElement({ x, y });
        }
    }

    // Show block editor in secondary sidebar
    async showBlockEditor(blockId) {
        // Track this editor for auto-refresh
        this._currentEditorType = 'block';
        this._currentEditorId = blockId;

        // Fetch fresh block data from the API to ensure we have current state
        let block;
        try {
            block = await this.api.getBlock(blockId);
            if (block.error) {
                console.warn('[Editor] Block not found for editor:', blockId, block.error);
                return;
            }
        } catch (err) {
            // Fallback to cached data if API fails
            block = this.cachedBlocks?.find(b => b.block_id === blockId);
            if (!block) {
                console.warn('[Editor] Block not found for editor:', blockId);
                return;
            }
        }

        const blockColor = this.getBlockColor(blockId);

        const content = `
            <div class="block-editor-panel" data-block-id="${blockId}" style="background: transparent;">
                ${renderHeaderId(blockId, blockColor)}
                ${renderBlockStateDisplay(block.state || 'free', 'cycle-block-state-btn')}
                ${renderTextInput('block-name-input', 'Block Name', block.name || '', 'Enter block name...')}
                ${renderReadOnlyValue('Track Count', `${block.track_count || 0} tiles`, 'text-track font-bold')}
                ${renderTileList(block.sorted_positions, {
                    entityType: 'block',
                    extendStartBtnId: 'extend-block-start-btn',
                    extendEndBtnId: 'extend-block-end-btn',
                    removeBtnClass: 'delete-block-tile-btn'
                })}
                ${renderNumberInput('block-length-input', 'Length (cm)', block.length_cm || 0, { min: 0, step: 1 })}
                ${renderNumberInput('block-radius-input', 'Min Radius (cm)', block.min_radius_cm || '', { min: 0, step: 1, placeholder: 'Not set' })}
                ${renderNumberInput('block-speed-input', 'Max Speed (km/h)', block.max_speed_kmh || '', { min: 0, step: 1, placeholder: 'Not set' })}
                ${renderConditional(block.occupied_by, 
                    renderStatusBadge('Occupied By', `🚂 ${block.occupied_by}`, 'status-display-danger')
                )}
                ${renderActionButtons('save-block-btn', 'delete-block-btn')}
            </div>
        `;

        this.showSecondarySidebar(`📦 Block Editor`, content);
        
        // Initialize event handlers after content is rendered
        setTimeout(() => this.initBlockEditorHandlers(blockId), 0);
    }

    // Initialize handlers for block editor in secondary sidebar
    async initBlockEditorHandlers(blockId) {
        const saveBtn = document.getElementById('save-block-btn');
        const deleteBtn = document.getElementById('delete-block-btn');
        const cycleBtn = document.getElementById('cycle-block-state-btn');
        const nameInput = document.getElementById('block-name-input');
        const lengthInput = document.getElementById('block-length-input');
        const radiusInput = document.getElementById('block-radius-input');
        const speedInput = document.getElementById('block-speed-input');

        // Cycle state button
        if (cycleBtn) {
            cycleBtn.onclick = async () => {
                try {
                    const result = await this.api.cycleBlockState(blockId);
                    await window.app.refreshNetwork();

                    // Update the status light and text directly without re-rendering
                    const newState = result.state || 'FREE';
                    const stateColor = newState === 'free' ? '#04d361' :
                                       newState === 'reserved' ? '#ffd93d' :
                                       newState === 'occupied' ? '#e83f5b' :
                                       newState === 'caution' ? '#ff9500' : '#888';

                    // Find and update the status light (first span in the state row)
                    const panel = document.querySelector(`.block-editor-panel[data-block-id="${blockId}"]`);
                    if (panel) {
                        const stateDisplay = panel.querySelector('#block-state-display');
                        if (stateDisplay) {
                            stateDisplay.textContent = newState.toUpperCase();
                            stateDisplay.style.color = stateColor;
                        }
                        // Update the light (sibling span before state display)
                        const stateRow = stateDisplay?.parentElement;
                        if (stateRow) {
                            const light = stateRow.querySelector('span:first-child');
                            if (light) {
                                light.style.background = stateColor;
                                light.style.boxShadow = `0 0 8px ${stateColor}`;
                            }
                        }
                    }
                } catch (err) {
                    console.error('Failed to cycle block state:', err);
                    window.showToast('Failed to cycle block state: ' + err.message, 'error', 5000);
                }
            };
        }

        // Save button
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (nameInput) {
                    updates.name = nameInput.value.trim();  // Allow empty string
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }
                if (radiusInput) {
                    const val = radiusInput.value.trim();
                    if (val === '') {
                        updates.min_radius_cm = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.min_radius_cm = numVal;
                        }
                    }
                }
                if (speedInput) {
                    const val = speedInput.value.trim();
                    if (val === '') {
                        updates.max_speed_kmh = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.max_speed_kmh = numVal;
                        }
                    }
                }

                console.log('[Editor] Saving block updates:', blockId, updates);
                try {
                    const result = await this.api.updateBlock(blockId, updates);
                    console.log('[Editor] Block save result:', result);
                    await window.app.refreshNetwork();
                    // Also refresh the blocks cache
                    await this.refreshBlocksList();
                    // Re-open to show updated values
                    this.showBlockEditor(blockId);
                    // Also refresh main properties panel if it shows this block
                    if (this.selectedElement?.block_section_id === blockId) {
                        await this.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save block properties:', err);
                    window.showToast('Failed to save block properties: ' + err.message, 'error', 5000);
                }
            };
        }

        // Delete button
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete block "${blockId}"? This will remove block assignment from all tracks.`)) {
                    return;
                }
                // Remember selected element before delete
                const selectedPos = this.selectedElement ? { x: this.selectedElement.x, y: this.selectedElement.y } : null;
                const wasShowingThisBlock = this.selectedElement?.block_section_id === blockId;
                try {
                    await this.api.deleteBlock(blockId);
                    await window.app.refreshNetwork();
                    this.hideSecondarySidebar();
                    // Refresh main properties panel if it was showing this block
                    if (wasShowingThisBlock && selectedPos) {
                        await this.selectElement(selectedPos);
                    }
                } catch (err) {
                    console.error('Failed to delete block:', err);
                    window.showToast('Failed to delete block: ' + err.message, 'error', 5000);
                }
            };
        }

        // Extend Start button
        const extendBlockStartBtn = document.getElementById('extend-block-start-btn');
        if (extendBlockStartBtn) {
            extendBlockStartBtn.onclick = () => {
                this.enterBlockExtendMode(blockId, 'start');
            };
        }

        // Extend End button
        const extendBlockEndBtn = document.getElementById('extend-block-end-btn');
        if (extendBlockEndBtn) {
            extendBlockEndBtn.onclick = () => {
                this.enterBlockExtendMode(blockId, 'end');
            };
        }

        // Tile delete buttons
        const deleteBlockTileBtns = document.querySelectorAll('.delete-block-tile-btn');
        deleteBlockTileBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            
            // Add hover effect
            btn.onmouseenter = () => {
                btn.style.background = '#e83f5b';
                btn.style.color = 'white';
            };
            btn.onmouseleave = () => {
                btn.style.background = 'transparent';
                btn.style.color = '#e83f5b';
            };
            
            // Click handler
            btn.onclick = async () => {
                try {
                    // Call the API to remove the tile (backend validates if it's an edge tile)
                    await this.api.removeBlockTiles(blockId, [[x, y]]);
                    await window.app.refreshNetwork();
                    
                    // Check if block still exists (might have been auto-deleted if last tile)
                    try {
                        const updatedBlock = await this.api.getBlock(blockId);
                        if (updatedBlock && !updatedBlock.error) {
                            // Re-open editor to show updated tile list
                            this.showBlockEditor(blockId);
                        } else {
                            // Block was deleted or no longer exists
                            this.hideSecondarySidebar();
                            window.showToast('Block deleted (last tile removed)', 'success', 5000);
                        }
                    } catch (e) {
                        // Block doesn't exist anymore
                        this.hideSecondarySidebar();
                        window.showToast('Block deleted (last tile removed)', 'success', 5000);
                    }
                    
                    // Refresh main properties panel if it was showing this tile
                    if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                        await this.selectElement({ x, y });
                    }
                    
                    window.showToast(`Tile (${x}, ${y}) removed from block`, 'success', 5000);
                } catch (err) {
                    console.error(`Failed to remove tile (${x}, ${y}) from block:`, err);
                    window.showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
                }
            };
        });
    }

    // Show station editor in secondary sidebar (similar to showBlockEditor)
    showStationEditor(stationId) {
        // Track this editor for auto-refresh
        this._currentEditorType = 'station';
        this._currentEditorId = stationId;

        const station = this.cachedNetwork?.stations?.find(s => s.station_id === stationId);
        if (!station) {
            console.warn('[Editor] Station not found for editor:', stationId);
            return;
        }

        // Get all blocks for dropdown
        const blocks = this.cachedNetwork?.blocks || [];

        const content = `
            <div class="station-editor-panel" data-station-id="${stationId}" style="background: transparent;">
                ${renderHeaderId(stationId, '#ffd93d')}
                ${renderTextInput('station-name-input', 'Station Name', station.name || '', 'Enter station name...')}
                ${renderNumberInput('station-length-input', 'Platform Length (cm)', station.length_cm || 0, { min: 0, step: 10 })}
                ${renderDropdown('station-side-input', 'Platform Side', station.platform_side, [
                    { value: 'both', label: 'Both Sides' },
                    { value: 'left', label: 'Left Side' },
                    { value: 'right', label: 'Right Side' }
                ])}
                ${renderNumberInput('station-speed-input', 'Top Speed (km/h)', station.top_speed_kmh || '', { min: 0, step: 5, placeholder: 'Not set' })}
                ${renderReadOnlyValue('Track Count', `${station.positions?.length || 0} tiles`, 'text-track font-bold')}
                ${renderTileListWithBlocks(station.sorted_positions, station.block_assignments_sorted, blocks, {
                    entityType: 'station',
                    extendStartBtnId: 'extend-start-btn',
                    extendEndBtnId: 'extend-end-btn',
                    removeBtnClass: 'delete-tile-btn',
                    assignBtnClass: 'assign-block-btn'
                })}
                ${renderConditional(station.orientation, 
                    renderReadOnlyValue('Orientation', station.orientation, 'text-info font-bold')
                )}
                ${renderActionButtons('save-station-btn', 'delete-station-btn')}
            </div>
        `;

        this.showSecondarySidebar('🚉 Station Editor', content);

        // Initialize event handlers after content is rendered
        setTimeout(() => this.initStationEditorHandlers(stationId), 0);
    }

    // Initialize handlers for station editor in secondary sidebar
    async initStationEditorHandlers(stationId) {
        const saveBtn = document.getElementById('save-station-btn');
        const deleteBtn = document.getElementById('delete-station-btn');
        const nameInput = document.getElementById('station-name-input');
        const lengthInput = document.getElementById('station-length-input');
        const sideInput = document.getElementById('station-side-input');
        const speedInput = document.getElementById('station-speed-input');

        // Save button
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (nameInput) {
                    updates.name = nameInput.value.trim() || '';
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }
                if (sideInput) {
                    updates.platform_side = sideInput.value;
                }
                if (speedInput) {
                    const val = speedInput.value.trim();
                    if (val === '') {
                        updates.top_speed_kmh = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.top_speed_kmh = numVal;
                        }
                    }
                }

                try {
                    await this.api.updateStation(stationId, updates);
                    await window.app.refreshNetwork();
                    // Re-open to show updated values
                    this.showStationEditor(stationId);
                    // Also refresh main properties panel if it shows this station
                    if (this.selectedElement?.station_id === stationId) {
                        await this.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                    window.showToast('Station properties saved', 'success', 'error', 5000);
                } catch (err) {
                    console.error('Failed to save station properties:', err);
                    window.showToast('Failed to save station properties: ' + err.message, 'error', 5000);
                }
            };
        }

        // Delete button
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete station "${stationId}"? This will remove the station from all tracks.`)) {
                    return;
                }
                // Remember selected element before delete
                const selectedPos = this.selectedElement ? { x: this.selectedElement.x, y: this.selectedElement.y } : null;
                const wasShowingThisStation = this.selectedElement?.station_id === stationId;
                try {
                    await this.api.deleteStation(stationId);
                    await window.app.refreshNetwork();
                    this.hideSecondarySidebar();
                    // Refresh main properties panel if it was showing this station
                    if (wasShowingThisStation && selectedPos) {
                        await this.selectElement(selectedPos);
                    }
                    window.showToast('Station deleted', 'success', 'error', 5000);
                } catch (err) {
                    console.error('Failed to delete station:', err);
                    window.showToast('Failed to delete station: ' + err.message, 'error', 5000);
                }
            };
        }

        // Extend Start button
        const extendStartBtn = document.getElementById('extend-start-btn');
        if (extendStartBtn) {
            extendStartBtn.onclick = () => {
                this.enterStationExtendMode(stationId, 'start');
            };
        }

        // Extend End button
        const extendEndBtn = document.getElementById('extend-end-btn');
        if (extendEndBtn) {
            extendEndBtn.onclick = () => {
                this.enterStationExtendMode(stationId, 'end');
            };
        }

        // Tile delete buttons
        const deleteTileBtns = document.querySelectorAll('.delete-tile-btn');
        deleteTileBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            
            // Add hover effect
            btn.onmouseenter = () => {
                btn.style.background = '#e83f5b';
                btn.style.color = 'white';
            };
            btn.onmouseleave = () => {
                btn.style.background = 'transparent';
                btn.style.color = '#e83f5b';
            };
            
            // Click handler
            btn.onclick = async () => {
                try {
                    // Call the API to remove the tile (backend validates if it's an edge tile)
                    await this.api.removeStationTile(stationId, x, y);
                    await window.app.refreshNetwork();
                    
                    // Check if station still exists (might have been auto-deleted if last tile)
                    // Use cached network since refreshNetwork() just updated it
                    const stationStillExists = this.cachedNetwork?.stations?.some(s => s.station_id === stationId);
                    
                    if (stationStillExists) {
                        // Re-open editor to show updated tile list
                        this.showStationEditor(stationId);
                    } else {
                        // Station was deleted (last tile removed)
                        this.hideSecondarySidebar();
                        window.showToast('Station deleted (last tile removed)', 'success', 5000);
                    }
                    
                    // Refresh main properties panel if it was showing this tile
                    if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                        await this.selectElement({ x, y });
                    }
                    
                    window.showToast(`Tile (${x}, ${y}) removed from station`, 'success', 5000);
                } catch (err) {
                    console.error(`Failed to remove tile (${x}, ${y}) from station:`, err);
                    window.showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
                }
            };
        });

        // Tile assign block buttons
        const assignBlockBtns = document.querySelectorAll('.assign-block-btn');
        assignBlockBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            const index = parseInt(btn.dataset.index);
            
            btn.onclick = async () => {
                await this.showBlockAssignmentDialog(stationId, x, y, index);
            };
        });
    }

    // Show dialog to assign a tile to a block
    async showBlockAssignmentDialog(stationId, x, y, tileIndex) {
        const station = this.cachedNetwork?.stations?.find(s => s.station_id === stationId);
        if (!station) return;

        const blocks = this.cachedNetwork?.blocks || [];
        const currentBlock = station.block_assignments_sorted?.[tileIndex] || null;

        // Build block options
        const blockOptions = blocks.map(b => ({
            value: b.block_id,
            label: `${b.block_id} (${b.state})`
        }));

        // Add "None" option
        blockOptions.unshift({ value: '', label: '-- No Block --' });

        // Create dialog HTML
        const dialogHtml = `
            <div style="background: #1a1a2e; border: 2px solid #00d4ff; border-radius: 8px; padding: 20px; max-width: 400px;">
                <h4 style="color: #00d4ff; margin: 0 0 16px 0;">Assign Tile to Block</h4>
                <div style="margin-bottom: 16px;">
                    <div style="color: #aaa; font-size: 14px;">Tile: <span style="color: #fff; font-family: monospace;">(${x}, ${y})</span></div>
                    ${currentBlock ? `<div style="color: #aaa; font-size: 14px; margin-top: 4px;">Current: <span style="color: #ffd93d;">${currentBlock}</span></div>` : ''}
                </div>
                <select id="block-assign-select" style="width: 100%; padding: 8px; background: #2a2a3e; color: #fff; border: 1px solid #3a3a4e; border-radius: 4px; margin-bottom: 16px;">
                    ${blockOptions.map(opt => 
                        `<option value="${opt.value}" ${opt.value === currentBlock ? 'selected' : ''}>${opt.label}</option>`
                    ).join('')}
                </select>
                <div style="display: flex; gap: 12px;">
                    <button id="block-assign-save" style="flex: 1; padding: 10px; background: #00d4ff; color: #0a0a0f; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">
                        Save
                    </button>
                    <button id="block-assign-cancel" style="padding: 10px 20px; background: transparent; color: #e83f5b; border: 1px solid #e83f5b; border-radius: 4px; cursor: pointer;">
                        Cancel
                    </button>
                </div>
            </div>
        `;

        // Show dialog as overlay
        const overlay = document.createElement('div');
        overlay.id = 'block-assign-overlay';
        overlay.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; z-index: 10000;';
        overlay.innerHTML = dialogHtml;
        document.body.appendChild(overlay);

        // Handle save
        const saveBtn = overlay.querySelector('#block-assign-save');
        const cancelBtn = overlay.querySelector('#block-assign-cancel');
        const select = overlay.querySelector('#block-assign-select');

        saveBtn.onclick = async () => {
            const selectedBlockId = select.value;
            
            try {
                if (selectedBlockId === '') {
                    // Unassign
                    if (currentBlock) {
                        await this.api.unassignTileFromBlock(stationId, x, y);
                        window.showToast(`Tile (${x}, ${y}) unassigned from block`, 'success', 5000);
                    }
                } else {
                    // Assign
                    await this.api.assignTileToBlock(stationId, x, y, selectedBlockId);
                    window.showToast(`Tile (${x}, ${y}) assigned to ${selectedBlockId}`, 'success', 5000);
                }
                
                // Refresh and re-open editor
                await window.app.refreshNetwork();
                this.showStationEditor(stationId);
                
                // Remove dialog
                document.body.removeChild(overlay);
            } catch (err) {
                console.error('Failed to assign tile to block:', err);
                window.showToast(`Failed to assign tile: ${err.message}`, 'error', 5000);
            }
        };

        // Handle cancel
        cancelBtn.onclick = () => {
            document.body.removeChild(overlay);
        };

        // Handle overlay click (close)
        overlay.onclick = (e) => {
            if (e.target === overlay) {
                document.body.removeChild(overlay);
            }
        };
    }

    /**
     * Calculate the next tile position for automatic extension.
     * Uses the edge tile's track configuration to determine the next position.
     * Platform tiles have exactly 2 exits - we extend to the exit that's NOT already in the platform.
     * 
     * @param {Array<[number, number]>} sortedPositions - Sorted station/block positions
     * @param {string} orientation - 'NS', 'EW', 'NE_SW', or 'NW_SE' (used for track bits validation)
     * @param {string} direction - 'start' or 'end'
     * @returns {{x: number, y: number, trackBits: number}} Next tile position and required track bits
     */
    calculateNextTile(sortedPositions, orientation, direction) {
        if (!sortedPositions || sortedPositions.length === 0) {
            throw new Error('No positions to extend from');
        }

        // Get the edge tile to extend from
        const edgeTile = direction === 'start' ? sortedPositions[0] : sortedPositions[sortedPositions.length - 1];
        const [edgeX, edgeY] = edgeTile;

        // Find the edge track in the cached network
        const edgeTrack = this.cachedNetwork.tracks.find(t => t.x === edgeX && t.y === edgeY);
        if (!edgeTrack) {
            throw new Error(`No track found at edge position (${edgeX}, ${edgeY})`);
        }

        // Get the exits of the edge tile
        const exits = this.getTrackExits(edgeTrack.track_bits);
        if (exits.length !== 2) {
            throw new Error(`Edge tile must have exactly 2 exits, found ${exits.length}`);
        }

        console.log('[Editor] Edge tile:', {x: edgeX, y: edgeY, trackBits: edgeTrack.track_bits, exits});

        // Convert exits to neighbor positions
        const neighbors = exits.map(exit => this.getNeighborPosition(edgeX, edgeY, exit));
        
        console.log('[Editor] Potential neighbors:', neighbors);

        // Check which neighbor is already in the platform
        const positionSet = new Set(sortedPositions.map(([x, y]) => `${x},${y}`));
        const availableNeighbors = neighbors.filter(([nx, ny]) => !positionSet.has(`${nx},${ny}`));

        if (availableNeighbors.length === 0) {
            throw new Error('Both neighbors are already in the platform - cannot extend');
        }

        // If both neighbors are available (single-tile station), use the direction parameter
        // and orientation to determine which way to extend
        let nextX, nextY;
        if (availableNeighbors.length > 1) {
            // Single-tile station: use direction and orientation to choose
            const [n1x, n1y] = availableNeighbors[0];
            const [n2x, n2y] = availableNeighbors[1];
            
            // Determine which neighbor matches the intended direction based on orientation
            if (orientation === 'NS') {
                // Start = north (higher Y), End = south (lower Y)
                nextX = edgeX;
                nextY = direction === 'start' ? Math.max(n1y, n2y) : Math.min(n1y, n2y);
            } else if (orientation === 'EW') {
                // Start = west (lower X), End = east (higher X)
                nextX = direction === 'start' ? Math.min(n1x, n2x) : Math.max(n1x, n2x);
                nextY = edgeY;
            } else if (orientation === 'NE_SW') {
                // Start = toward NE (higher x-y), End = toward SW (lower x-y)
                const diff1 = n1x - n1y;
                const diff2 = n2x - n2y;
                [nextX, nextY] = direction === 'start' ? 
                    (diff1 > diff2 ? [n1x, n1y] : [n2x, n2y]) :
                    (diff1 < diff2 ? [n1x, n1y] : [n2x, n2y]);
            } else if (orientation === 'NW_SE') {
                // Start = toward NW (higher x+y), End = toward SE (lower x+y)
                const sum1 = n1x + n1y;
                const sum2 = n2x + n2y;
                [nextX, nextY] = direction === 'start' ? 
                    (sum1 > sum2 ? [n1x, n1y] : [n2x, n2y]) :
                    (sum1 < sum2 ? [n1x, n1y] : [n2x, n2y]);
            }
            
            console.log('[Editor] Single-tile station, chose direction:', direction, 'position:', {x: nextX, y: nextY});
        } else {
            // Multi-tile station: only one available neighbor
            [nextX, nextY] = availableNeighbors[0];
        }

        // Now determine which track bits the next tile should have
        // It must match the platform's orientation
        const trackBits = this.getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY);

        console.log('[Editor] Calculated next tile:', {x: nextX, y: nextY, trackBits});

        return { x: nextX, y: nextY, trackBits };
    }

    /**
     * Get track exits from track bits
     * @param {number} trackBits - Track configuration bits
     * @returns {Array<string>} Array of exit directions ('N', 'E', 'S', 'W')
     */
    getTrackExits(trackBits) {
        const exits = [];
        const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;
        
        if (trackBits & TRACK_NS) { exits.push('N', 'S'); }
        if (trackBits & TRACK_EW) { exits.push('E', 'W'); }
        if (trackBits & TRACK_NE) { exits.push('N', 'E'); }
        if (trackBits & TRACK_NW) { exits.push('N', 'W'); }
        if (trackBits & TRACK_SE) { exits.push('S', 'E'); }
        if (trackBits & TRACK_SW) { exits.push('S', 'W'); }
        
        // Remove duplicates
        return [...new Set(exits)];
    }

    /**
     * Get neighbor position in a given direction
     * @param {number} x - Current X coordinate
     * @param {number} y - Current Y coordinate
     * @param {string} direction - Direction ('N', 'E', 'S', 'W')
     * @returns {[number, number]} Neighbor position [x, y]
     */
    getNeighborPosition(x, y, direction) {
        const offsets = {
            'N': [0, 1],
            'E': [1, 0],
            'S': [0, -1],
            'W': [-1, 0]
        };
        const [dx, dy] = offsets[direction];
        return [x + dx, y + dy];
    }

    /**
     * Determine track bits for next tile based on orientation and positions
     * @param {string} orientation - Platform orientation
     * @param {number} nextX - Next tile X
     * @param {number} nextY - Next tile Y
     * @param {number} edgeX - Edge tile X
     * @param {number} edgeY - Edge tile Y
     * @returns {number} Track bits for the next tile
     */
    getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY) {
        const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;
        
        if (orientation === 'NS') {
            return TRACK_NS;
        } else if (orientation === 'EW') {
            return TRACK_EW;
        } else if (orientation === 'NE_SW') {
            // Zigzag: determine from direction of movement
            const dx = nextX - edgeX;
            const dy = nextY - edgeY;
            if (dx === 1 && dy === 0) return TRACK_SW; // Moving east
            if (dx === 0 && dy === 1) return TRACK_NE; // Moving north
            if (dx === -1 && dy === 0) return TRACK_NE; // Moving west
            if (dx === 0 && dy === -1) return TRACK_SW; // Moving south
        } else if (orientation === 'NW_SE') {
            // Zigzag: determine from direction of movement
            const dx = nextX - edgeX;
            const dy = nextY - edgeY;
            if (dx === 1 && dy === 0) return TRACK_SE; // Moving east
            if (dx === 0 && dy === 1) return TRACK_SE; // Moving north
            if (dx === -1 && dy === 0) return TRACK_NW; // Moving west
            if (dx === 0 && dy === -1) return TRACK_NW; // Moving south
        }
        
        throw new Error(`Cannot determine track bits for orientation ${orientation}`);
    }

    /**
     * Detect orientation from sorted positions
     * @param {Array<[number, number]>} sortedPositions - Sorted positions
     * @returns {string} Orientation: 'NS', 'EW', 'NE_SW', or 'NW_SE'
     */
    detectOrientation(sortedPositions) {
        if (!sortedPositions || sortedPositions.length < 1) {
            throw new Error('Need at least 1 position to detect orientation');
        }

        // For single-tile blocks/stations, detect from track bits
        if (sortedPositions.length === 1) {
            const [x, y] = sortedPositions[0];
            const track = this.cachedNetwork.tracks.find(t => t.x === x && t.y === y);
            if (!track) {
                throw new Error(`No track found at position (${x}, ${y})`);
            }

            // Map track bits to orientation groups
            const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;
            
            if (track.track_bits & TRACK_NS) return 'NS';
            if (track.track_bits & TRACK_EW) return 'EW';
            if (track.track_bits & (TRACK_NE | TRACK_SW)) return 'NE_SW';
            if (track.track_bits & (TRACK_NW | TRACK_SE)) return 'NW_SE';
            
            throw new Error('Cannot determine orientation from track bits');
        }

        // Check if all positions are on same X (NS) or same Y (EW)
        const allSameX = sortedPositions.every(([x, _]) => x === sortedPositions[0][0]);
        const allSameY = sortedPositions.every(([_, y]) => y === sortedPositions[0][1]);

        if (allSameX) return 'NS';
        if (allSameY) return 'EW';

        // For diagonals, check if positions are sorted by x-y (NE_SW) or x+y (NW_SE)
        // NE_SW zigzag: x-y increases along the path
        // NW_SE zigzag: x+y increases along the path
        
        const sums = sortedPositions.map(([x, y]) => x + y);
        const diffs = sortedPositions.map(([x, y]) => x - y);

        // Check if diffs are monotonically increasing (NE_SW)
        let diffsMonotonic = true;
        for (let i = 1; i < diffs.length; i++) {
            if (diffs[i] <= diffs[i-1]) {
                diffsMonotonic = false;
                break;
            }
        }

        // Check if sums are monotonically increasing (NW_SE)
        let sumsMonotonic = true;
        for (let i = 1; i < sums.length; i++) {
            if (sums[i] <= sums[i-1]) {
                sumsMonotonic = false;
                break;
            }
        }

        if (diffsMonotonic) {
            return 'NE_SW';
        } else if (sumsMonotonic) {
            return 'NW_SE';
        }

        // Fallback: use variance to decide
        const sumVariance = Math.max(...sums) - Math.min(...sums);
        const diffVariance = Math.max(...diffs) - Math.min(...diffs);

        if (diffVariance > sumVariance) {
            return 'NE_SW';
        } else {
            return 'NW_SE';
        }
    }

    // Automatically extend station (no click required)
    async enterStationExtendMode(stationId, direction) {
        console.log(`[Editor] Auto-extending station: ${stationId} (${direction})`);
        
        try {
            // Get station data
            const station = this.cachedNetwork?.stations?.find(s => s.station_id === stationId);
            if (!station) {
                throw new Error('Station not found');
            }

            // Get orientation and sorted positions
            const orientation = station.orientation || station.initial_orientation;
            const sortedPositions = station.sorted_positions || station.positions;

            console.log('[Editor] Station data:', {
                stationId,
                orientation,
                sortedPositions: JSON.stringify(sortedPositions),
                direction
            });
            
            // Log track bits for all positions to understand the pattern
            sortedPositions.forEach((pos, idx) => {
                const track = this.cachedNetwork.tracks.find(t => t.x === pos[0] && t.y === pos[1]);
                console.log(`[Editor] Position ${idx}: (${pos[0]}, ${pos[1]}) x+y=${pos[0]+pos[1]} track_bits=${track?.track_bits}`);
            });

            if (!orientation) {
                throw new Error('Cannot determine station orientation');
            }

            // Calculate next tile automatically
            const nextTile = this.calculateNextTile(sortedPositions, orientation, direction);
            console.log(`[Editor] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`);

            // Validate the tile can be extended
            const canExtend = await this.api.canExtendStation(stationId, nextTile.x, nextTile.y);
            
            if (!canExtend.can_extend) {
                window.showToast(`Cannot extend ${direction}: ${canExtend.reason}`, 'error', 5000);
                return;
            }
            
            // Extend the station
            await this.api.extendStation(stationId, [[nextTile.x, nextTile.y]]);
            await window.app.refreshNetwork();
            
            // Refresh station editor
            this.showStationEditor(stationId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === nextTile.x && this.selectedElement?.y === nextTile.y) {
                await this.selectElement({ x: nextTile.x, y: nextTile.y });
            }
            
            window.showToast(`Station extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
        } catch (err) {
            console.error('Failed to auto-extend station:', err);
            window.showToast(`Failed to extend station: ${err.message}`, 'error', 5000);
        }
    }

    // Exit station extend mode
    exitStationExtendMode() {
        if (this.stationExtendMode?.active) {
            console.log('[Editor] Exiting station extend mode');
            this.stationExtendMode = { active: false };
            document.body.style.cursor = 'default';
            
            // Restore header
            const secondarySidebar = document.getElementById('secondary-sidebar-content');
            if (secondarySidebar) {
                const header = secondarySidebar.closest('.secondary-sidebar')?.querySelector('h3');
                if (header) {
                    header.textContent = '🚉 Station Editor';
                    header.style.color = '';
                }
            }
        }
    }

    // Handle click in station extend mode
    async handleStationExtendClick(x, y) {
        if (!this.stationExtendMode?.active) return false;
        
        const { stationId, direction } = this.stationExtendMode;
        
        try {
            // Validate the tile can be extended
            const canExtend = await this.api.canExtendStation(stationId, x, y);
            
            if (!canExtend.can_extend) {
                window.showToast(`Cannot extend: ${canExtend.reason}`, 'error', 5000);
                return true; // Handled, but failed
            }
            
            // Extend the station
            await this.api.extendStation(stationId, [[x, y]]);
            await window.app.refreshNetwork();
            
            // Exit extend mode
            this.exitStationExtendMode();
            
            // Refresh station editor
            this.showStationEditor(stationId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                await this.selectElement({ x, y });
            }
            
            window.showToast(`Tile (${x}, ${y}) added to station`, 'success', 5000);
            return true;
        } catch (err) {
            console.error('Failed to extend station:', err);
            window.showToast(`Failed to extend station: ${err.message}`, 'error', 5000);
            return true;
        }
    }

    // Automatically extend block (no click required)
    async enterBlockExtendMode(blockId, direction) {
        console.log(`[Editor] Auto-extending block: ${blockId} (${direction})`);
        
        try {
            // Get block data
            const block = await this.api.getBlock(blockId);
            if (!block || block.error) {
                throw new Error('Block not found');
            }

            // Get sorted positions
            const sortedPositions = block.sorted_positions || block.track_positions;

            if (!sortedPositions || sortedPositions.length === 0) {
                throw new Error('Block has no positions');
            }

            // Detect orientation from sorted positions
            const orientation = this.detectOrientation(sortedPositions);
            console.log(`[Editor] Detected block orientation: ${orientation}`);

            // Calculate next tile automatically
            const nextTile = this.calculateNextTile(sortedPositions, orientation, direction);
            console.log(`[Editor] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`);

            // Extend the block (use force=false to let backend validate)
            await this.api.extendBlock(blockId, [[nextTile.x, nextTile.y]], false);
            await window.app.refreshNetwork();
            
            // Refresh block editor
            this.showBlockEditor(blockId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === nextTile.x && this.selectedElement?.y === nextTile.y) {
                await this.selectElement({ x: nextTile.x, y: nextTile.y });
            }
            
            window.showToast(`Block extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
        } catch (err) {
            console.error('Failed to auto-extend block:', err);
            window.showToast(`Failed to extend block: ${err.message}`, 'error', 5000);
        }
    }

    // Exit block extend mode
    exitBlockExtendMode() {
        if (this.blockExtendMode?.active) {
            console.log('[Editor] Exiting block extend mode');
            this.blockExtendMode = { active: false };
            document.body.style.cursor = 'default';
            
            // Restore header
            const secondarySidebar = document.getElementById('secondary-sidebar-content');
            if (secondarySidebar) {
                const header = secondarySidebar.closest('.secondary-sidebar')?.querySelector('h3');
                if (header) {
                    header.textContent = '📦 Block Editor';
                    header.style.color = '';
                }
            }
        }
    }

    // Handle click in block extend mode
    async handleBlockExtendClick(x, y) {
        if (!this.blockExtendMode?.active) return false;
        
        const { blockId, direction } = this.blockExtendMode;
        
        try {
            // Validate the tile can be extended
            const canExtend = await this.api.canExtendBlock(blockId, x, y);
            
            if (!canExtend.can_extend) {
                // Check if it requires force flags
                if (canExtend.requires_force) {
                    window.showToast(`Tile has signal - use force mode to extend (not yet implemented)`, 'warning', 5000);
                } else if (canExtend.requires_force_overwrite) {
                    window.showToast(`Tile in another block - use force overwrite (not yet implemented)`, 'warning', 5000);
                } else {
                    window.showToast(`Cannot extend: ${canExtend.reason}`, 'error', 5000);
                }
                return true; // Handled, but failed
            }
            
            // Extend the block
            await this.api.extendBlock(blockId, [[x, y]]);
            await window.app.refreshNetwork();
            
            // Exit extend mode
            this.exitBlockExtendMode();
            
            // Refresh block editor
            this.showBlockEditor(blockId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                await this.selectElement({ x, y });
            }
            
            window.showToast(`Tile (${x}, ${y}) added to block`, 'success', 5000);
            return true;
        } catch (err) {
            console.error('Failed to extend block:', err);
            window.showToast(`Failed to extend block: ${err.message}`, 'error', 5000);
            return true;
        }
    }

    // Show train editor in secondary sidebar
    async showTrainEditor(trainId) {
        // Track this editor for auto-refresh
        this._currentEditorType = 'train';
        this._currentEditorId = trainId;

        // Fetch fresh train data
        let train;
        try {
            train = await this.api.getTrain(trainId);
        } catch (err) {
            console.error('[Editor] Failed to fetch train:', err);
            window.showToast(`Failed to load train: ${err.message}`, 'error', 5000);
            return;
        }

        if (!train) {
            console.warn('[Editor] Train not found:', trainId);
            window.showToast(`Train ${trainId} not found`, 'error', 5000);
            return;
        }

        // Determine status badge CSS class
        const statusClass = train.status === 'RUNNING' ? 'status-display-success' :
                           train.status === 'WAITING' ? 'status-display-warning' :
                           train.status === 'BLOCKED' ? 'status-display-danger' : 'status-display-info';

        const content = `
            <div class="train-editor-panel" data-train-id="${trainId}" style="background: transparent;">
                ${renderHeaderId(trainId, '#8257e5')}
                ${renderStatusBadge('Status', train.status || 'STOPPED', statusClass)}
                ${renderReadOnlyValue('Position', 
                    train.position ? `(${train.position.x}, ${train.position.y})` : 'Unknown', 
                    'text-info', true
                )}
                ${renderReadOnlyValue('Direction', train.direction || 'EAST', 'font-bold text-station')}
                ${renderReadOnlyValue('Current Speed', `${(train.current_speed || 0).toFixed(1)} km/h`, 'font-bold text-track')}
                ${renderNumberInput('train-max-speed-input', 'Max Speed (km/h)', train.max_speed || 100, { min: 1, max: 500, step: 1 })}
                ${renderNumberInput('train-length-input', 'Length (cm)', train.length_cm || 0, { min: 0, step: 10 })}
                ${renderConditional(train.block_section_id,
                    renderReadOnlyValue('Block Section', train.block_section_id, 'text-block', true)
                )}
                ${renderConditional(train.schedule && train.schedule.length > 0, `
                    <div class="property-item">
                        <label class="property-label">Schedule</label>
                        <div class="bg-input rounded-lg text-xs text-muted" style="max-height: 120px; overflow-y: auto; border: 1px solid #3a3a4e; padding: 8px; font-family: monospace;">
                            ${train.schedule.map((stop, idx) => `${idx + 1}. ${stop}`).join('<br>')}
                        </div>
                    </div>
                `)}
                ${renderActionButtons('save-train-btn', 'delete-train-btn')}
            </div>
        `;

        this.showSecondarySidebar('🚄 Train Editor', content);

        // Initialize event handlers after content is rendered
        setTimeout(() => this.initTrainEditorHandlers(trainId), 0);
    }

    // Initialize handlers for train editor in secondary sidebar
    async initTrainEditorHandlers(trainId) {
        const saveBtn = document.getElementById('save-train-btn');
        const deleteBtn = document.getElementById('delete-train-btn');
        const maxSpeedInput = document.getElementById('train-max-speed-input');
        const lengthInput = document.getElementById('train-length-input');

        // Save button
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (maxSpeedInput) {
                    const val = parseFloat(maxSpeedInput.value);
                    if (!isNaN(val) && val > 0) {
                        updates.max_speed = val;
                    }
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }

                try {
                    await this.api.updateTrain(trainId, updates);
                    await window.app.refreshNetwork();
                    // Re-open to show updated values
                    this.showTrainEditor(trainId);
                    window.showToast('Train properties saved', 'success', 5000);
                } catch (err) {
                    console.error('Failed to save train properties:', err);
                    window.showToast('Failed to save train properties: ' + err.message, 'error', 5000);
                }
            };
        }

        // Delete button
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete train "${trainId}"? This action cannot be undone.`)) {
                    return;
                }
                try {
                    await this.api.deleteTrain(trainId);
                    await window.app.refreshNetwork();
                    this.hideSecondarySidebar();
                    window.showToast('Train deleted', 'success', 5000);
                } catch (err) {
                    console.error('Failed to delete train:', err);
                    window.showToast('Failed to delete train: ' + err.message, 'error', 5000);
                }
            };
        }
    }

    setupEventListeners() {
        // Tool selection
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');

                const newTool = e.currentTarget.dataset.tool;
                const oldTool = this.selectedTool;

                // Auto-toggle block debug when entering/leaving block tools
                const blockDebugCheckbox = document.getElementById('debug-show-blocks');
                const isBlockTool = ['block', 'extend-block'].includes(newTool);
                const wasBlockTool = ['block', 'extend-block'].includes(oldTool);

                if (isBlockTool && !wasBlockTool) {
                    // Entering block tool mode - enable block colors only (no debug info)
                    this._savedBlockDebugState = blockDebugCheckbox?.checked;
                    this.renderer.showBlockColorsOnly = true;
                    window.app.refreshNetwork(); // Refresh to show block colors
                } else if (!isBlockTool && wasBlockTool) {
                    // Leaving block tool mode - restore normal rendering
                    this.renderer.showBlockColorsOnly = false;
                    window.app.refreshNetwork(); // Refresh to update display
                }

                // Handle route tool special actions
                if (newTool === 'path') {
                    this.enterRouteToolMode();
                } else if (newTool === 'extend-path') {
                    this.enterExtendRouteMode();
                } else if (newTool === 'cancel-path') {
                    this.enterCancelRouteMode();
                } else if (newTool === 'paths-panel') {
                    this.showPathsPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'add-train') {
                    this.showAddTrainDialog();
                    // Don't change selected tool - keep select tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'train-panel') {
                    this.showTrainsPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (oldTool === 'path') {
                    // Leaving path tool mode
                    this.exitRouteToolMode();
                } else if (oldTool === 'extend-path') {
                    // Leaving extend-path tool mode
                    this.exitExtendRouteMode();
                } else if (oldTool === 'cancel-path') {
                    // Leaving cancel-path tool mode
                    this.exitCancelRouteMode();
                }

                this.selectedTool = newTool;
                this.previewPath = null;
                
                // Auto-expand the category containing the selected tool
                this.expandCategoryForTool(newTool);
            });
        });

        // Collapsible tool categories
        this.setupCollapsibleCategories();

        const canvas = this.renderer.canvas;

        // Mouse down - start drag or pan
        canvas.addEventListener('mousedown', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);

            // Middle mouse, select tool, or route-related tools = pan
            const pathTools = ['path', 'extend-path', 'cancel-path'];
            if (e.button === 1 || (e.button === 0 && this.selectedTool === 'select') ||
                (e.button === 0 && pathTools.includes(this.selectedTool))) {
                this.isPanning = true;
                this.panStart = { x, y };
                this.hasPanned = false; // Track if actual panning occurred
                // Initialize last mouse position for delta calculation
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            }
            // Left mouse + path-based tools = start drag
            else if (e.button === 0 && ['track', 'block', 'eraser', 'extend-block', 'extend-station', 'remove-station-tile', 'remove-block-tile'].includes(this.selectedTool)) {
                this.isDraggingBuild = true;
                this.dragStart = gridPos;
                this.dragEnd = gridPos;
                // Clear cached path for new drag operation
                this.lastValidPreviewPath = null;
                this.lastValidPreviewTracks = null;
            }
            // Left click with signal/station = single click placement
            else if (e.button === 0 && ['signal', 'station'].includes(this.selectedTool)) {
                this.handleSingleClickPlace(gridPos);
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
                
                // If mouse moved more than a small threshold, mark as actual panning
                if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
                    this.hasPanned = true;
                }
                
                this.renderer.panX += dx;
                this.renderer.panY += dy;
                // Redraw the full network, not just the grid
                if (this.renderer.currentNetwork) {
                    this.renderer.drawNetwork(this.renderer.currentNetwork);
                } else {
                    this.renderer.draw();
                }
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            }
            else if (this.isDraggingBuild && this.dragStart) {
                this.dragEnd = gridPos;
                this.updatePreviewPath();
            }
            // Hover preview for signal/station tools
            else if (['signal', 'station'].includes(this.selectedTool)) {
                this.updateHoverPreview(gridPos);
            }
        });

        // Mouse up - commit drag build
        canvas.addEventListener('mouseup', async (e) => {
            if (this.isPanning) {
                this.isPanning = false;
            }
            else if (this.isDraggingBuild && this.dragStart && this.dragEnd) {
                // Stop dragging state immediately to prevent updates during async operations
                this.isDraggingBuild = false;

                // Check if this is a single click (no drag movement) for eraser
                const isSingleClick = this.dragStart.x === this.dragEnd.x && this.dragStart.y === this.dragEnd.y;

                if (this.selectedTool === 'eraser' && isSingleClick) {
                    // Single click eraser: two-stage deletion
                    await this.handleErase(this.dragStart);
                } else if (['remove-station-tile', 'remove-block-tile'].includes(this.selectedTool) && isSingleClick) {
                    // Single click removal tools
                    await this.handleRemovalClick(this.dragStart);
                } else if (['eraser', 'block', 'track', 'extend-block', 'extend-station', 'remove-station-tile', 'remove-block-tile'].includes(this.selectedTool)) {
                    // Drag operation: commit the build/erase/block/removal path
                    await this.commitDragBuild();
                }

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

            await this.handleRightClick(gridPos);
        });

        // Zoom with mouse-centered zooming and pan compensation
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();

            // Get mouse position in screen coordinates
            const rect = canvas.getBoundingClientRect();
            const mouseScreenX = e.clientX - rect.left;
            const mouseScreenY = e.clientY - rect.top;

            // Calculate mouse position in world coordinates BEFORE zoom
            const worldPosBeforeZoom = this.renderer.screenToGrid(mouseScreenX, mouseScreenY);

            // Apply zoom
            const zoomIntensity = 0.1;
            const delta = e.deltaY < 0 ? 1 : -1;
            const oldScale = this.renderer.scale;
            const newScale = oldScale + (delta * zoomIntensity);

            // Clamp zoom level
            if (newScale <= 0.1 || newScale >= 5) return;

            // Update scale
            this.renderer.scale = newScale;

            // Calculate where the mouse world position would appear on screen with new scale
            // (without adjusting pan)
            const newTileSize = this.renderer.gridSize * newScale;
            const screenXWithoutPanAdjust = this.renderer.centerX + this.renderer.panX +
                ((worldPosBeforeZoom.x + worldPosBeforeZoom.y + 1) * newTileSize / 2);
            const screenYWithoutPanAdjust = this.renderer.centerY + this.renderer.panY +
                ((worldPosBeforeZoom.x - worldPosBeforeZoom.y) * newTileSize / 2);

            // Calculate pan adjustment to keep mouse world position under cursor
            const panAdjustX = mouseScreenX - screenXWithoutPanAdjust;
            const panAdjustY = mouseScreenY - screenYWithoutPanAdjust;

            // Apply pan adjustment
            this.renderer.panX += panAdjustX;
            this.renderer.panY += panAdjustY;

            // Redraw with cached network
            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            } else {
                this.renderer.draw();
            }
            document.getElementById('status-zoom').textContent = `${Math.round(newScale * 100)}%`;
        });

        // Click to select element for properties panel
        canvas.addEventListener('click', async (e) => {
            // Don't handle click if user was panning
            if (this.hasPanned) {
                this.hasPanned = false; // Reset for next interaction
                return;
            }
            
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);
            
            // Check if in station extend mode
            if (this.stationExtendMode?.active) {
                const handled = await this.handleStationExtendClick(gridPos.x, gridPos.y);
                if (handled) return;
            }
            
            // Check if in block extend mode
            if (this.blockExtendMode?.active) {
                const handled = await this.handleBlockExtendClick(gridPos.x, gridPos.y);
                if (handled) return;
            }
            
            // Check if in path tool mode
            if (this.selectedTool === 'path') {
                await this.handleRouteToolClick(gridPos);
                return;
            }
            
            // Check if in extend-path tool mode
            if (this.selectedTool === 'extend-path') {
                await this.handleExtendRouteToolClick(gridPos);
                return;
            }
            
            // Check if in cancel-path tool mode
            if (this.selectedTool === 'cancel-path') {
                await this.handleCancelRouteToolClick(gridPos);
                return;
            }
            
            // Only handle selection with select tool
            if (this.selectedTool !== 'select') return;
            
            this.selectElement(gridPos);
        });

        // Global keyboard events for shift key (diagonal build mode)
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Shift' && !this.shiftKeyHeld) {
                this.shiftKeyHeld = true;
                this.diagonalBuildMode = true;
                // Update preview if currently dragging
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
            }
        });

        document.addEventListener('keyup', (e) => {
            if (e.key === 'Shift') {
                this.shiftKeyHeld = false;
                this.diagonalBuildMode = false;
                // Update preview if currently dragging
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
            }
        });

        // ESC key - exit extend mode or return to select tool
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                // Check if we're in a modal or input field
                const inModal = document.querySelector('.block-modal-overlay, .modal-overlay') !== null;
                const inInput = document.activeElement.tagName === 'INPUT' ||
                               document.activeElement.tagName === 'TEXTAREA' ||
                               document.activeElement.tagName === 'SELECT';

                if (inModal || inInput) {
                    return; // Let modal/input handlers deal with it
                }

                // Exit station extend mode if active
                if (this.stationExtendMode?.active) {
                    this.exitStationExtendMode();
                    window.showToast('Extend mode cancelled', 'info', 3000);
                    return;
                }

                // Exit block extend mode if active
                if (this.blockExtendMode?.active) {
                    this.exitBlockExtendMode();
                    window.showToast('Extend mode cancelled', 'info', 3000);
                    return;
                }

                // Exit path tool mode if active
                if (this.pathToolState?.active) {
                    this.exitRouteToolMode();
                    window.showToast('Route creation cancelled', 'info', 3000);
                    return;
                }

                // Switch to select tool
                if (this.selectedTool !== 'select') {
                    const selectBtn = document.querySelector('.tool-btn[data-tool="select"]');
                    if (selectBtn) {
                        selectBtn.click(); // This triggers the existing tool switch logic
                    }
                }
            }
        });
    }

    // === Block Manager Methods ===

    setupBlockManager() {
        // Block Manager UI has been removed - blocks are now managed via
        // the secondary sidebar block editor. Keep the block cache refresh.
        this.refreshBlocksList();
    }

    async refreshBlocksList() {
        try {
            const blocks = await this.api.getBlocks();
            this.cachedBlocks = blocks;
            // No longer renders to UI - Block Manager panel removed
        } catch (err) {
            console.error('[Editor] Failed to refresh blocks:', err);
        }
    }

    // renderBlocksList removed - Block Manager UI no longer exists

    selectBlock(blockId) {
        this.selectedBlockId = blockId;
        // Block selection now only highlights tracks on canvas
        // UI list no longer exists
    }

    async editBlockDialog(blockId) {
        try {
            const blockData = await this.api.getBlock(blockId);
            if (blockData.error) {
                window.showToast(blockData.error, 'error', 5000);
                return;
            }

            // Show block properties modal
            const result = await this.showBlockPropertiesModal({
                blockId: blockId,
                blockData: blockData
            });

            if (!result) return; // User cancelled

            // Update block with new values
            await this.api.updateBlock(blockId, result);
            await this.refreshBlocksList();
        } catch (err) {
            console.error('[Editor] Failed to update block:', err);
            window.showToast('Failed to update block: ' + err.message, 'error', 5000);
        }
    }

    async deleteBlock(blockId) {
        if (!confirm(`Delete block "${blockId}"?\nTracks will be unassigned from this block.`)) return;

        try {
            await this.api.deleteBlock(blockId);
            if (this.selectedBlockId === blockId) {
                this.selectedBlockId = null;
            }
            await this.refreshBlocksList();
            await window.app.refreshNetwork();
        } catch (err) {
            console.error('[Editor] Failed to delete block:', err);
            window.showToast('Failed to delete block: ' + err.message, 'error', 5000);
        }
    }

    getBlockColor(blockId) {
        // Match renderer's HSL hash-based color generation
        if (!blockId) return '#666';
        let hash = 0;
        for (let i = 0; i < blockId.length; i++) {
            hash = blockId.charCodeAt(i) + ((hash << 5) - hash);
        }
        const hue = Math.abs(hash) % 360;
        return `hsl(${hue}, 70%, 50%)`;
    }

    // === Block Modal Dialog Methods ===

    /**
     * Show a modal dialog for block creation or extension
     * @param {Object} options - Modal options
     * @param {string} options.mode - 'create' or 'extend'
     * @param {number} options.trackCount - Number of tracks in path
     * @param {string[]} options.existingBlocks - List of existing block IDs in path (for extend mode)
     * @returns {Promise<Object|null>} - User's choice or null if cancelled
     */
    showBlockModal(options) {
        return new Promise((resolve) => {
            const { mode, trackCount, existingBlocks = [] } = options;

            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'block-modal-overlay';

            let modalContent = '';

            if (mode === 'create') {
                // Create new block form
                const defaultId = `BLK_${Date.now().toString(16).toUpperCase().slice(-6)}`;
                modalContent = `
                    <div class="block-modal">
                        <h3>Create New Block</h3>
                        <div class="modal-body">
                            <p class="modal-info">Creating block with <strong>${trackCount}</strong> tracks</p>
                            <div class="form-group">
                                <label for="modal-block-id">Block ID</label>
                                <input type="text" id="modal-block-id" value="${defaultId}" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-block-name">Block Name (optional)</label>
                                <input type="text" id="modal-block-name" placeholder="e.g., Main Line West" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-max-speed">Max Speed km/h (optional)</label>
                                <input type="number" id="modal-max-speed" placeholder="e.g., 60" min="0" step="1">
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Create Block</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            } else if (mode === 'extend') {
                // Extend existing block
                if (existingBlocks.length === 0) {
                    // No existing blocks - shouldn't happen in extend mode
                    resolve(null);
                    return;
                } else if (existingBlocks.length === 1) {
                    // Single block to extend
                    const blockId = existingBlocks[0];
                    const block = this.cachedBlocks?.find(b => b.block_id === blockId);
                    modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Extending block <strong>${blockId}</strong></p>
                                ${block?.name ? `<p class="modal-subinfo">${block.name}</p>` : ''}
                                <p class="modal-detail">Adding <strong>${trackCount}</strong> tracks to this block.</p>
                                ${block ? `<p class="modal-detail">Current size: ${block.track_count || 0} tracks</p>` : ''}
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" data-block-id="${blockId}">Extend Block</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
                } else {
                    // Multiple blocks - user must choose
                    const blockOptions = existingBlocks.map(blockId => {
                        const block = this.cachedBlocks?.find(b => b.block_id === blockId);
                        return `
                            <div class="block-selection-item" data-block-id="${blockId}">
                                <input type="radio" name="extend-block" value="${blockId}" id="block-${blockId}">
                                <label for="block-${blockId}">
                                    <span class="block-id">${blockId}</span>
                                    ${block?.name ? `<span class="block-name">${block.name}</span>` : ''}
                                    ${block?.station_names ? `<span class="block-name" style="color: #eebb00; margin-left: 5px;">[${block.station_names.join(', ')}]</span>` : ''}
                                    <span class="block-track-count">${block?.track_count || 0} tracks</span>
                                </label>
                            </div>
                        `;
                    }).join('');

                    modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Path covers <strong>${existingBlocks.length}</strong> blocks</p>
                                <p class="modal-warning">Select which block to extend. Other block assignments will be overwritten.</p>
                                <div class="block-selection-list">
                                    ${blockOptions}
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" disabled>Extend Selected</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
                }
            }

            overlay.innerHTML = modalContent;
            document.body.appendChild(overlay);

            // Focus first input
            const firstInput = overlay.querySelector('input[type="text"], input[type="number"]');
            if (firstInput) {
                setTimeout(() => firstInput.focus(), 50);
            }

            // Handle radio button selection for multi-block extend
            const radioButtons = overlay.querySelectorAll('input[name="extend-block"]');
            const confirmBtn = overlay.querySelector('#modal-confirm');

            radioButtons.forEach(radio => {
                radio.addEventListener('change', () => {
                    confirmBtn.disabled = false;
                    confirmBtn.dataset.blockId = radio.value;
                });
            });

            // Handle confirm
            confirmBtn?.addEventListener('click', () => {
                if (mode === 'create') {
                    const blockId = overlay.querySelector('#modal-block-id')?.value?.trim();
                    const name = overlay.querySelector('#modal-block-name')?.value?.trim();
                    const maxSpeed = overlay.querySelector('#modal-max-speed')?.value;

                    if (!blockId) {
                        window.showToast('Block ID is required', 'error', 5000);
                        return;
                    }

                    overlay.remove();
                    resolve({
                        action: 'create',
                        blockId,
                        name: name || '',
                        maxSpeed: maxSpeed ? parseFloat(maxSpeed) : null
                    });
                } else if (mode === 'extend') {
                    const blockId = confirmBtn.dataset.blockId;
                    if (!blockId) {
                        window.showToast('Please select a block', 'error', 5000);
                        return;
                    }

                    overlay.remove();
                    resolve({
                        action: 'extend',
                        blockId
                    });
                }
            });

            // Handle cancel
            overlay.querySelector('#modal-cancel')?.addEventListener('click', () => {
                overlay.remove();
                resolve(null);
            });

            // Handle overlay click (close)
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.remove();
                    resolve(null);
                }
            });

            // Handle Escape key
            const handleEscape = (e) => {
                if (e.key === 'Escape') {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            };
            document.addEventListener('keydown', handleEscape);

            // Handle Enter key for create mode
            if (mode === 'create') {
                const handleEnter = (e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        confirmBtn?.click();
                    }
                };
                overlay.addEventListener('keydown', handleEnter);
            }
        });
    }

    // === Station Modal Dialog Method ===

    /**
     * Show a modal dialog for station creation or editing
     * @param {Object} options - Modal options
     * @param {string} options.mode - 'create' or 'edit'
     * @param {number} options.x - X coordinate (for create mode)
     * @param {number} options.y - Y coordinate (for create mode)
     * @param {string} options.existingBlock - Block ID that will be split (if any)
     * @param {Object} options.station - Existing station data (for edit mode)
     * @returns {Promise<Object|null>} - User's input or null if cancelled
     */
    showStationModal(options) {
        return new Promise((resolve) => {
            const { mode, x, y, existingBlock, station } = options;

            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'block-modal-overlay';

            // Generate default station ID
            const defaultId = station?.station_id || `STA_${Date.now().toString(16).toUpperCase().slice(-6)}`;

            let modalContent = '';

            if (mode === 'create') {
                // Create new station form
                modalContent = `
                    <div class="block-modal">
                        <h3>Create New Station</h3>
                        <div class="modal-body">
                            ${existingBlock ? `
                                <p class="modal-warning">
                                    ⚠️ This will split block <strong>${existingBlock}</strong> into separate blocks
                                </p>
                            ` : ''}
                            <div class="form-group">
                                <label for="modal-station-id">Station ID</label>
                                <input type="text" id="modal-station-id" value="${defaultId}" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-station-name">Station Name</label>
                                <input type="text" id="modal-station-name" placeholder="e.g., Central Station" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-length-cm">Platform Length (cm)</label>
                                <input type="number" id="modal-length-cm" placeholder="e.g., 500" min="0" step="10" value="500">
                                <small>Physical length of the platform in centimeters</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-platform-side">Platform Side</label>
                                <select id="modal-platform-side">
                                    <option value="both">Both Sides</option>
                                    <option value="left">Left Side Only</option>
                                    <option value="right">Right Side Only</option>
                                </select>
                                <small>Which side(s) of the track have platforms</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-top-speed">Top Speed (km/h)</label>
                                <input type="number" id="modal-top-speed" placeholder="e.g., 40" min="0" step="5">
                                <small>Optional speed limit through the station</small>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Create Station</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            } else {
                // Edit existing station form
                modalContent = `
                    <div class="block-modal">
                        <h3>Edit Station: ${station.station_id}</h3>
                        <div class="modal-body">
                            <div class="form-group">
                                <label>Station ID</label>
                                <input type="text" value="${station.station_id}" disabled>
                                <small>Station ID cannot be changed</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-station-name">Station Name</label>
                                <input type="text" id="modal-station-name" value="${station.name || ''}" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-length-cm">Platform Length (cm)</label>
                                <input type="number" id="modal-length-cm" value="${station.length_cm || 0}" min="0" step="10">
                            </div>
                            <div class="form-group">
                                <label for="modal-platform-side">Platform Side</label>
                                <select id="modal-platform-side">
                                    <option value="both" ${station.platform_side === 'both' ? 'selected' : ''}>Both Sides</option>
                                    <option value="left" ${station.platform_side === 'left' ? 'selected' : ''}>Left Side Only</option>
                                    <option value="right" ${station.platform_side === 'right' ? 'selected' : ''}>Right Side Only</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label for="modal-top-speed">Top Speed (km/h)</label>
                                <input type="number" id="modal-top-speed" value="${station.top_speed_kmh || ''}" min="0" step="5">
                            </div>
                            <div class="form-group">
                                <label>Tiles</label>
                                <div class="info-text">${station.tile_count || 0} tiles</div>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Save Changes</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            }

            overlay.innerHTML = modalContent;
            document.body.appendChild(overlay);

            // Get form elements
            const stationIdInput = overlay.querySelector('#modal-station-id');
            const stationNameInput = overlay.querySelector('#modal-station-name');
            const lengthInput = overlay.querySelector('#modal-length-cm');
            const platformSideSelect = overlay.querySelector('#modal-platform-side');
            const topSpeedInput = overlay.querySelector('#modal-top-speed');
            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            // Handle confirm button
            confirmBtn.addEventListener('click', () => {
                const name = stationNameInput.value.trim();
                const lengthCm = parseFloat(lengthInput.value) || 0;
                const platformSide = platformSideSelect.value;
                const topSpeedKmh = topSpeedInput.value ? parseFloat(topSpeedInput.value) : null;

                // Validation
                if (!name) {
                    window.showToast('Station name is required', 'error', 5000);
                    stationNameInput.focus();
                    return;
                }

                if (mode === 'create') {
                    const stationId = stationIdInput.value.trim();
                    if (!stationId) {
                        window.showToast('Station ID is required', 'error', 5000);
                        stationIdInput.focus();
                        return;
                    }
                }

                if (lengthCm < 0) {
                    window.showToast('Length cannot be negative', 'error', 5000);
                    lengthInput.focus();
                    return;
                }

                if (topSpeedKmh !== null && topSpeedKmh <= 0) {
                    window.showToast('Top speed must be positive', 'error', 5000);
                    topSpeedInput.focus();
                    return;
                }

                // Clean up and resolve
                overlay.remove();
                document.removeEventListener('keydown', handleEscape);

                resolve({
                    stationId: mode === 'create' ? stationIdInput.value.trim() : station.station_id,
                    name,
                    lengthCm,
                    platformSide,
                    topSpeedKmh
                });
            });

            // Handle cancel button
            cancelBtn.addEventListener('click', () => {
                overlay.remove();
                document.removeEventListener('keydown', handleEscape);
                resolve(null);
            });

            // Handle overlay click (close)
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            });

            // Handle Escape key
            const handleEscape = (e) => {
                if (e.key === 'Escape') {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            };
            document.addEventListener('keydown', handleEscape);

            // Handle Enter key for create mode
            const handleEnter = (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    confirmBtn?.click();
                }
            };
            overlay.addEventListener('keydown', handleEnter);

            // Auto-focus first input
            setTimeout(() => {
                if (mode === 'create') {
                    stationNameInput?.focus();
                } else {
                    stationNameInput?.focus();
                    stationNameInput?.select();
                }
            }, 50);
        });
    }

    /**
     * Show block properties modal for editing block details
     * @param {Object} options - Modal options
     * @param {string} options.blockId - The block ID to edit
     * @param {Object} options.blockData - The block data from API
     * @returns {Promise<Object|null>} Block updates or null if cancelled
     */
    showBlockPropertiesModal(options) {
        return new Promise((resolve) => {
            const { blockId, blockData } = options;

            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            const modal = document.createElement('div');
            modal.className = 'modal';
            modal.innerHTML = `
                <h3>Edit Block Properties</h3>
                <div class="form-group">
                    <label>Block ID</label>
                    <input type="text" id="modal-block-id" value="${blockId}" readonly
                           style="background: #1a1a2e; color: #888; cursor: not-allowed;">
                </div>
                <div class="form-group">
                    <label>Name</label>
                    <input type="text" id="modal-block-name" value="${blockData.name || ''}"
                           placeholder="Block name">
                </div>
                <div class="form-group">
                    <label>Length (cm)</label>
                    <input type="number" id="modal-block-length" value="${blockData.length_cm || 0}"
                           min="0" step="0.1" placeholder="0.0">
                </div>
                <div class="form-group">
                    <label>Max Speed (km/h) <span style="color: #888;">(optional)</span></label>
                    <input type="number" id="modal-block-speed" value="${blockData.max_speed_kmh || ''}"
                           min="0" step="1" placeholder="Leave empty for no limit">
                </div>

                <div class="form-group">
                    <label>Current State</label>
                    <div class="state-indicator state-${blockData.state || 'free'}">
                        ${(blockData.state || 'free').toUpperCase()}
                    </div>
                </div>

                <div class="form-group">
                    <label>Monitoring Signals</label>
                    <div class="signal-list" id="monitoring-signals-list">
                        ${this.renderSignalList(blockData.monitoring_signals || [])}
                    </div>
                </div>

                <div class="button-group">
                    <button class="btn btn-primary" id="save-block-btn">Save</button>
                    <button class="btn btn-secondary" id="cancel-block-btn">Cancel</button>
                </div>
            `;

            overlay.appendChild(modal);
            document.body.appendChild(overlay);

            const nameInput = modal.querySelector('#modal-block-name');
            const lengthInput = modal.querySelector('#modal-block-length');
            const speedInput = modal.querySelector('#modal-block-speed');
            const saveBtn = modal.querySelector('#save-block-btn');
            const cancelBtn = modal.querySelector('#cancel-block-btn');

            // Focus first input
            setTimeout(() => nameInput.focus(), 50);

            // Handle save
            saveBtn.addEventListener('click', () => {
                const name = nameInput.value.trim();
                const lengthCm = parseFloat(lengthInput.value) || 0;
                const maxSpeedKmh = speedInput.value.trim() ? parseFloat(speedInput.value) : null;

                if (lengthCm < 0) {
                    window.showToast('Length cannot be negative', 'error', 5000);
                    return;
                }

                if (maxSpeedKmh !== null && maxSpeedKmh <= 0) {
                    window.showToast('Max speed must be positive', 'error', 5000);
                    return;
                }

                document.body.removeChild(overlay);
                resolve({
                    name,
                    length_cm: lengthCm,
                    max_speed_kmh: maxSpeedKmh
                });
            });

            // Handle cancel
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(null);
            });

            // Handle Enter key
            [nameInput, lengthInput, speedInput].forEach(input => {
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        saveBtn.click();
                    } else if (e.key === 'Escape') {
                        cancelBtn.click();
                    }
                });
            });
        });
    }

    /**
     * Render a list of signals for display in modals
     * @param {Object[]} signals - Array of signal details
     * @returns {string} HTML string for signal list
     */
    renderSignalList(signals) {
        if (!signals || signals.length === 0) {
            return '<div class="signal-item" style="color: #888; font-style: italic;">No signals</div>';
        }

        return signals.map(signal => `
            <div class="signal-item">
                <span style="color: #00ff88; font-weight: bold;">${signal.signal_id || 'Unknown'}</span>
                <span style="color: #888;"> → ${signal.detection_direction || 'N/A'}</span>
                <span class="signal-state-badge state-${signal.state || 'red'}">${(signal.state || 'red').toUpperCase()}</span>
            </div>
        `).join('');
    }

    /**
     * Invalidate signals in a path by clearing their monitored_block_id
     * @param {Object[]} signalsInPath - Array of signal positions from validateBlockPath
     */
    async invalidateSignalsInPath(signalsInPath) {
        if (!signalsInPath || signalsInPath.length === 0) return;

        for (const signal of signalsInPath) {
            try {
                // Get signal at this position
                const signalData = await this.api.getSignalAt(signal.x, signal.y);
                if (signalData && signalData.signal_id) {
                    // Clear the monitored_block_id to invalidate the signal
                    await this.api.updateSignal(signalData.signal_id, {
                        monitored_block_id: null
                    });
                }
            } catch (err) {
                console.warn(`[Editor] Failed to invalidate signal at (${signal.x}, ${signal.y}):`, err);
            }
        }
    }

    // === End Block Manager Methods ===

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
                // Use diagonal tracks if shift key is held
                this.previewTracks = this.diagonalBuildMode
                    ? pathToDiagonalTracks(this.previewPath, this.cachedNetwork)
                    : pathToTracks(this.previewPath, this.cachedNetwork);
                // Cache this valid path
                this.lastValidPreviewPath = this.previewPath;
                this.lastValidPreviewTracks = this.previewTracks;
            } else {
                // Destination blocked - use last valid path
                this.previewPath = this.lastValidPreviewPath;
                this.previewTracks = this.lastValidPreviewTracks;
            }
        }
        else if (['block', 'extend-block', 'extend-station', 'eraser', 'remove-station-tile', 'remove-block-tile'].includes(this.selectedTool)) {
            // Use existing-only pathfinder for block assignment, extend-block, extend-station, eraser, and removal tools
            const existingTracks = this.cachedNetwork ? this.cachedNetwork.tracks : [];
            const foundPath = this.existingOnlyPathfinder.findPathOnExistingTracks(
                this.dragStart.x, this.dragStart.y,
                this.dragEnd.x, this.dragEnd.y,
                existingTracks
            );

            if (foundPath) {
                this.previewPath = foundPath;
                this.lastValidPreviewPath = foundPath;
            } else {
                this.previewPath = this.lastValidPreviewPath;
            }
            this.previewTracks = null;
        }
        else {
            // Single placement for other tools
            this.previewPath = [this.dragEnd];
            this.previewTracks = null;
        }

        // Redraw with preview
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
            // Draw actual track geometry preview if available
            if (this.previewTracks && this.previewTracks.length > 0) {
                this.renderer.drawPreviewTracks(this.previewTracks);
            }
            // Draw eraser and removal tools preview in red
            else if (['eraser', 'remove-station-tile', 'remove-block-tile'].includes(this.selectedTool) && this.previewPath) {
                this.renderer.drawEraserPreviewPath(this.previewPath);
            }
            // Draw block preview in purple (for block, extend-block, and extend-station tools)
            else if (['block', 'extend-block', 'extend-station'].includes(this.selectedTool) && this.previewPath) {
                this.renderer.drawBlockPreviewPath(this.previewPath);
            }
            else if (this.previewPath) {
                this.renderer.drawPreviewPath(this.previewPath);
            }
        }
    }

    async commitDragBuild() {
        if (!this.previewPath || this.previewPath.length === 0) {
            return;
        }

        try {
            if (this.selectedTool === 'track') {
                // Use diagonal tracks if shift key is held
                const tracks = this.diagonalBuildMode
                    ? pathToDiagonalTracks(this.previewPath, this.cachedNetwork)
                    : pathToTracks(this.previewPath, this.cachedNetwork);
                await this.api.addTracksBulk(tracks);
            }
            else if (this.selectedTool === 'block') {
                // Create new block - all tiles must have NO block assigned
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Validate the path for create mode
                const tracks = this.cachedNetwork?.tracks || [];
                const validation = validateBlockPath(this.previewPath, tracks, 'create');

                if (!validation.valid) {
                    const errorMsg = 'Cannot create block:\n' + validation.errors.join('\n');
                    window.showToast(errorMsg, 'error', 5000);
                    console.error('[Editor] Block creation validation failed:', validation.errors);
                    return;
                }

                // Show warnings about signals but allow proceeding
                if (validation.warnings.length > 0) {
                    const proceed = confirm(
                        'Warning:\n' + validation.warnings.join('\n') + '\n\nProceed anyway?'
                    );
                    if (!proceed) return;
                }

                // Show modal dialog for block creation
                const result = await this.showBlockModal({
                    mode: 'create',
                    trackCount: this.previewPath.length
                });

                if (!result) return; // Cancelled

                try {
                    // Create the new block
                    await this.api.createBlock(result.blockId, result.name || '');

                    // Set max speed if provided
                    if (result.maxSpeed) {
                        await this.api.updateBlock(result.blockId, {
                            max_speed_kmh: result.maxSpeed
                        });
                    }

                    // Assign tracks to block
                    const positions = this.previewPath.map(p => [p.x, p.y]);
                    await this.api.addTracksToBlock(result.blockId, positions);

                    // Refresh block list to show updated track count
                    await this.refreshBlocksList();
                } catch (err) {
                    console.error('[Editor] Failed to create block:', err);
                    window.showToast('Failed to create block: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'extend-block') {
                // Extend existing block - must start on tile with block
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Validate the path for extend mode
                const tracks = this.cachedNetwork?.tracks || [];
                const validation = validateBlockPath(this.previewPath, tracks, 'extend');

                if (!validation.valid) {
                    const errorMsg = 'Cannot extend block:\n' + validation.errors.join('\n');
                    window.showToast(errorMsg, 'error', 5000);
                    console.error('[Editor] Block extension validation failed:', validation.errors);
                    return;
                }

                // Check if there are signals in the path - these will be INVALIDATED
                const hasSignals = validation.signalsInPath.length > 0;
                if (hasSignals) {
                    const signalCount = validation.signalsInPath.length;
                    const proceed = confirm(
                        `Warning: Path contains ${signalCount} signal(s).\n\n` +
                        'These signals will be INVALIDATED and need reconfiguration.\n' +
                        '(They will show an orange warning triangle)\n\n' +
                        'Proceed anyway?'
                    );
                    if (!proceed) return;
                }

                // Build a track map for looking up block assignments
                const trackMap = new Map();
                tracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));

                // Calculate actual new tiles (excluding tiles already in the target block)
                // The target block is the first block in the path (where extension starts)
                const targetBlockId = validation.blocksInPath.length > 0 ? validation.blocksInPath[0] : null;
                let newTileCount = this.previewPath.length;
                if (targetBlockId) {
                    newTileCount = this.previewPath.filter(pos => {
                        const track = trackMap.get(`${pos.x},${pos.y}`);
                        // Count as new if no track or track is not already in the target block
                        return !track || track.block_section_id !== targetBlockId;
                    }).length;
                }

                // Show modal dialog for block extension with correct new tile count
                const result = await this.showBlockModal({
                    mode: 'extend',
                    trackCount: newTileCount,
                    existingBlocks: validation.blocksInPath
                });

                if (!result) return; // Cancelled

                try {
                    // Use the extend API with force=true if there are signals
                    // Use forceOverwrite=true if extending into tracks belonging to other blocks
                    const positions = this.previewPath.map(p => [p.x, p.y]);
                    const hasOtherBlocks = validation.blocksInPath.length > 1 ||
                        (validation.blocksInPath.length === 1 && validation.blocksInPath[0] !== result.blockId);
                    const apiResult = await this.api.extendBlock(result.blockId, positions, hasSignals, hasOtherBlocks);

                    // Show info about invalidated signals if any
                    if (apiResult.invalidated_signals && apiResult.invalidated_signals.length > 0) {
                        const count = apiResult.invalidated_signals.length;
                        window.showToast(`Block extended. ${count} signal(s) need reconfiguration.`, 'warning');
                    }

                    // Refresh block list to show updated track count
                    await this.refreshBlocksList();
                } catch (err) {
                    console.error('[Editor] Failed to extend block:', err);
                    window.showToast('Failed to extend block: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'extend-station') {
                // Extend existing station - must start on tile with station
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Find stations that intersect with the path
                const tracks = this.cachedNetwork?.tracks || [];
                const stations = this.cachedNetwork?.stations || [];
                const stationsInPath = new Set();

                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (track && track.station_id) {
                        stationsInPath.add(track.station_id);
                    }
                }

                if (stationsInPath.size === 0) {
                    window.showToast('No station found in path. Start the drag on an existing station.', 'error', 5000);
                    return;
                }

                // Get the station ID to extend (use first station found, which should be at drag start)
                let stationId;
                if (stationsInPath.size === 1) {
                    // Only one station, use it
                    stationId = Array.from(stationsInPath)[0];
                } else {
                    // Multiple stations - find the one at the drag start point
                    const startTrack = tracks.find(t => t.x === this.dragStart.x && t.y === this.dragStart.y);
                    if (startTrack && startTrack.station_id) {
                        stationId = startTrack.station_id;
                    } else {
                        // Fallback: use the first station found
                        stationId = Array.from(stationsInPath)[0];
                    }

                    const station = stations.find(s => s.station_id === stationId);
                }

                try {
                    // Get positions that don't already belong to the station
                    const station = stations.find(s => s.station_id === stationId);
                    const stationPositions = new Set(
                        station?.positions?.map(p => `${p[0]},${p[1]}`) || []
                    );

                    const newPositions = this.previewPath
                        .filter(p => !stationPositions.has(`${p.x},${p.y}`))
                        .map(p => [p.x, p.y]);

                    if (newPositions.length === 0) {
                        window.showToast('All tiles are already part of the station.', 'error', 5000);
                        return;
                    }

                    await this.api.extendStation(stationId, newPositions);

                    // Show success toast
                    window.showToast(`Station extended with ${newPositions.length} tile(s)`, 'success');
                } catch (err) {
                    console.error('[Editor] Failed to extend station:', err);
                    window.showToast('Failed to extend station: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'eraser') {
                // Batch delete all tracks in the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const positions = this.previewPath.map(p => [p.x, p.y]);
                await this.api.deleteTracksBulk(positions);
            }
            else if (this.selectedTool === 'remove-station-tile') {
                // Remove station tiles along the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const tracks = this.cachedNetwork?.tracks || [];
                let successCount = 0;
                let errorCount = 0;
                let errors = [];

                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (!track || !track.station_id) {
                        errors.push(`No station at (${pos.x}, ${pos.y})`);
                        errorCount++;
                        continue;
                    }

                    try {
                        await this.api.removeStationTile(track.station_id, pos.x, pos.y);
                        successCount++;
                    } catch (err) {
                        errors.push(`(${pos.x}, ${pos.y}): ${err.message}`);
                        errorCount++;
                    }
                }

                if (successCount > 0) {
                    window.showToast(`Removed ${successCount} station tile(s)`, 'success');
                }
                if (errorCount > 0) {
                    window.showToast(`Failed to remove ${errorCount} tile(s):\n${errors.slice(0, 3).join('\n')}`, 'error', 5000);
                }
            }
            else if (this.selectedTool === 'remove-block-tile') {
                // Remove block tiles along the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const tracks = this.cachedNetwork?.tracks || [];
                
                // Group positions by block
                const blockGroups = new Map();
                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (track && track.block_section_id) {
                        if (!blockGroups.has(track.block_section_id)) {
                            blockGroups.set(track.block_section_id, []);
                        }
                        blockGroups.get(track.block_section_id).push([pos.x, pos.y]);
                    }
                }

                if (blockGroups.size === 0) {
                    window.showToast('No block tiles in selected path', 'error', 3000);
                    return;
                }

                let totalSuccess = 0;
                let totalErrors = [];

                // Remove tiles from each block
                for (const [blockId, positions] of blockGroups.entries()) {
                    try {
                        const result = await this.api.removeBlockTiles(blockId, positions);
                        totalSuccess += result.removed || 0;
                        if (result.errors && result.errors.length > 0) {
                            totalErrors.push(...result.errors);
                        }
                    } catch (err) {
                        totalErrors.push(`Block ${blockId}: ${err.message}`);
                    }
                }

                if (totalSuccess > 0) {
                    window.showToast(`Removed ${totalSuccess} block tile(s)`, 'success');
                }
                if (totalErrors.length > 0) {
                    window.showToast(`Errors:\n${totalErrors.slice(0, 3).join('\n')}`, 'error', 5000);
                }
            }

            await this.refreshAndSelect(null);  // Refresh without selecting specific element
        } catch (err) {
            console.error('[Editor] Failed to commit build:', err);
        }
    }

    /**
     * Unified refresh helper - ensures single source of truth pattern:
     * 1. Await API refresh to get latest data
     * 2. Optionally refresh properties panel for a specific element
     * @param {Object|null} gridPos - Position to select and show in properties, or null to just refresh
     */
    async refreshAndSelect(gridPos) {
        await window.app.refreshNetwork();
        if (gridPos) {
            await this.selectElement(gridPos);
        }
    }

    async handleErase(gridPos) {
        try {
            const existingTrack = this.cachedNetwork?.tracks?.find(
                t => t.x === gridPos.x && t.y === gridPos.y
            );

            if (!existingTrack) {
                return;
            }

            // Check if tile has objects (signal or station)
            const hasSignal = existingTrack.signal_id || existingTrack.signal_state || existingTrack.type === 'signal';
            const hasStation = existingTrack.station_id || existingTrack.station_name || existingTrack.type === 'station';

            if (hasSignal || hasStation) {
                // First click: remove objects only, keep track

                // Build confirmation message
                let items = [];
                if (hasSignal) items.push(`Signal ${existingTrack.signal_id || 'at this location'}`);
                if (hasStation) items.push(`Station ${existingTrack.station_name || existingTrack.station_id || 'at this location'}`);

                const confirmed = confirm(`Delete ${items.join(' and ')}?\n\nThe track will remain.`);
                if (!confirmed) return;

                if (hasSignal) {
                    // Use the signal-specific deletion API
                    if (existingTrack.signal_id) {
                        await this.api.deleteSignal(existingTrack.signal_id);
                    } else {
                        // Fallback to position-based deletion
                        await this.api.deleteSignalAt(gridPos.x, gridPos.y);
                    }
                }
                if (hasStation) {
                    // Use the station-specific deletion API
                    if (existingTrack.station_id) {
                        await this.api.deleteStation(existingTrack.station_id);
                    }
                }
            } else {
                // Second click (no objects): remove track entirely
                await this.api.deleteTrack(gridPos.x, gridPos.y);
            }

            await this.refreshAndSelect(null);  // Refresh without selecting (track may be gone)
        } catch (err) {
            console.error('[Editor] Failed to erase:', err);
        }
    }

    async handleRemovalClick(gridPos) {
        try {
            const existingTrack = this.cachedNetwork?.tracks?.find(
                t => t.x === gridPos.x && t.y === gridPos.y
            );

            if (!existingTrack) {
                window.showToast('No track at this position', 'error', 3000);
                return;
            }

            if (this.selectedTool === 'remove-station-tile') {
                if (!existingTrack.station_id) {
                    window.showToast('No station at this position', 'error', 3000);
                    return;
                }

                try {
                    await this.api.removeStationTile(existingTrack.station_id, gridPos.x, gridPos.y);
                    window.showToast('Station tile removed', 'success');
                } catch (err) {
                    window.showToast(`Failed to remove station tile: ${err.message}`, 'error', 5000);
                }
            }
            else if (this.selectedTool === 'remove-block-tile') {
                if (!existingTrack.block_section_id) {
                    window.showToast('No block at this position', 'error', 3000);
                    return;
                }

                try {
                    const result = await this.api.removeBlockTiles(
                        existingTrack.block_section_id,
                        [[gridPos.x, gridPos.y]]
                    );
                    
                    if (result.removed > 0) {
                        window.showToast('Block tile removed', 'success');
                    } else if (result.errors && result.errors.length > 0) {
                        window.showToast(`Cannot remove: ${result.errors[0]}`, 'error', 5000);
                    }
                } catch (err) {
                    window.showToast(`Failed to remove block tile: ${err.message}`, 'error', 5000);
                }
            }

            await this.refreshAndSelect(null);
        } catch (err) {
            console.error('[Editor] Failed to handle removal click:', err);
        }
    }

    // Single click placement for signal/station tools
    async handleSingleClickPlace(gridPos) {
        const existingTrack = this.cachedNetwork?.tracks?.find(
            t => t.x === gridPos.x && t.y === gridPos.y
        );

        if (!existingTrack) {
            window.showToast(`Cannot place ${this.selectedTool}: No track at this position`, 'error', 5000);
            return;
        }

        if (this.selectedTool === 'signal') {
            // Check if already has a station
            if (existingTrack.station_name) {
                window.showToast('Cannot place signal on a station', 'error', 5000);
                return;
            }

            // First, validate signal placement via API (synchronous - always get fresh data)
            try {
                const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

                if (!validation.can_place) {
                    window.showToast(validation.error || 'Cannot place signal at this position', 'error', 5000);
                    return;
                }

                // Only use cached preview direction if it's for the SAME position
                // This prevents race conditions where user clicks before validation updates
                // available_directions are detection directions (where trains travel TO)
                let detectionDirection;
                if (this.previewSignalDirection &&
                    this._lastValidatedPosition?.x === gridPos.x &&
                    this._lastValidatedPosition?.y === gridPos.y &&
                    validation.available_directions?.includes(this.previewSignalDirection)) {
                    detectionDirection = this.previewSignalDirection;
                } else {
                    // Use first available detection direction from fresh API validation
                    detectionDirection = validation.available_directions?.[0] || validation.valid_directions?.[0];
                }

                if (!detectionDirection) {
                    window.showToast('No valid detection direction available for signal placement', 'error', 5000);
                    return;
                }

                // Create signal via API with detection_direction
                await this.api.createSignal(
                    gridPos.x,
                    gridPos.y,
                    detectionDirection,  // detection_direction: where trains travel TO
                    'block',  // signal_type
                    null,     // name
                    true      // auto_connect
                );

                // Reset preview direction after placing signal
                this.previewSignalDirection = null;
                this.previewSignalDirectionIndex = 0;
                this._lastValidatedPosition = null;
            } catch (err) {
                console.error('[Editor] Signal creation failed:', err);
                window.showToast('Failed to create signal: ' + err.message, 'error', 5000);
                return;
            }
        } else if (this.selectedTool === 'station') {
            // Validate station placement via API
            try {
                const validation = await this.api.validateStationPlacement(gridPos.x, gridPos.y);

                if (!validation.can_place) {
                    window.showToast(validation.error || 'Cannot place station at this position', 'error', 5000);
                    return;
                }

                // Show station modal to get station properties
                const stationData = await this.showStationModal({
                    mode: 'create',
                    x: gridPos.x,
                    y: gridPos.y,
                    existingBlock: validation.existing_block
                });

                if (!stationData) return; // User cancelled

                // Create station via API
                await this.api.createStation(
                    gridPos.x,
                    gridPos.y,
                    stationData.name,
                    stationData.lengthCm,
                    stationData.platformSide,
                    stationData.topSpeedKmh,
                    stationData.stationId
                );
            } catch (err) {
                console.error('[Editor] Station creation failed:', err);
                window.showToast('Failed to create station: ' + err.message, 'error', 5000);
                return;
            }
        }

        // Refresh and select the newly placed element
        await this.refreshAndSelect(gridPos);
        // Clear hover preview
        this.hoverPosition = null;
        this.hoverValid = false;
    }

    // Update hover preview for signal/station tools
    // Uses async API validation for accurate signal placement checking
    updateHoverPreview(gridPos) {
        if (!['signal', 'station'].includes(this.selectedTool)) {
            this.hoverPosition = null;
            this.hoverValid = false;
            return;
        }

        this.hoverPosition = gridPos;

        // Check if this tile has valid track for placement
        const existingTrack = this.cachedNetwork?.tracks?.find(
            t => t.x === gridPos.x && t.y === gridPos.y
        );

        // Quick local validation first
        if (!existingTrack) {
            this.hoverValid = false;
            this.previewSignalDirection = null;
            this.validSignalDirections = [];
            this.drawWithHoverPreview();
            return;
        }

        if (this.selectedTool === 'signal') {
            // Debounced API validation for accurate signal placement
            this.validateSignalPreviewAsync(gridPos, existingTrack);
        } else if (this.selectedTool === 'station') {
            // Station validation:
            // - Cannot be a switch
            // - Cannot already have signal
            this.hoverValid = !existingTrack.is_switch && !existingTrack.signal_state;
            this.previewSignalDirection = null;
            this.drawWithHoverPreview();
        }
    }

    // Async API validation for signal preview - debounced to avoid flooding server
    async validateSignalPreviewAsync(gridPos, existingTrack) {
        // Cancel any pending validation
        if (this.signalValidationTimeout) {
            clearTimeout(this.signalValidationTimeout);
        }

        // Show pending state with local validation first
        const trackBits = existingTrack.track_bits || 0;
        const localDirections = getValidSignalDirections(trackBits);
        const hasStation = !!existingTrack.station_name;

        if (localDirections.length === 0 || hasStation) {
            this.hoverValid = false;
            this.previewSignalDirection = null;
            this.validSignalDirections = [];
            this.drawWithHoverPreview();
            return;
        }

        // Immediately show local validation (prevents flickering)
        // Only update if this is a new position or we don't have directions yet
        if (this.validSignalDirections.length === 0 ||
            this._lastValidatedPosition?.x !== gridPos.x ||
            this._lastValidatedPosition?.y !== gridPos.y) {
            this.validSignalDirections = localDirections;
            this.hoverValid = localDirections.length > 0;
            if (this.hoverValid && !this.previewSignalDirection) {
                this.previewSignalDirectionIndex = 0;
                this.previewSignalDirection = localDirections[0];
            }
            this.drawWithHoverPreview();
        }

        // Debounce the API call (50ms)
        this.signalValidationTimeout = setTimeout(async () => {
            try {
                // Call API to get accurate validation including existing signal check
                const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

                // Make sure we're still hovering on the same tile
                if (!this.hoverPosition || this.hoverPosition.x !== gridPos.x || this.hoverPosition.y !== gridPos.y) {
                    return;  // User moved away, ignore result
                }

                // Track that we've validated this position
                this._lastValidatedPosition = { x: gridPos.x, y: gridPos.y };

                if (!validation.can_place) {
                    // Signal cannot be placed (e.g., all directions already have signals)
                    this.hoverValid = false;
                    this.previewSignalDirection = null;
                    this.validSignalDirections = [];
                } else {
                    // Get available directions from API (directions without existing signals)
                    const availableDirections = validation.available_directions || [];

                    // Use available_directions to show which directions can be placed
                    this.validSignalDirections = availableDirections;
                    this.hoverValid = availableDirections.length > 0;

                    // Set initial preview direction
                    if (this.hoverValid && availableDirections.length > 0) {
                        if (!this.previewSignalDirection || !availableDirections.includes(this.previewSignalDirection)) {
                            this.previewSignalDirectionIndex = 0;
                            this.previewSignalDirection = availableDirections[0];
                        }
                    } else {
                        this.previewSignalDirection = null;
                    }
                }

                this.drawWithHoverPreview();
            } catch (err) {
                console.warn('[Editor] Signal validation API failed, using local validation:', err);
                // Fallback to local validation - already set above, no need to redraw
            }
        }, 50);
    }

    // Check if a tool is actively being used that needs hover preview preservation
    // Used by App to avoid flickering during network polling
    isToolActive() {
        // Tools that show hover previews
        const previewTools = ['signal', 'station'];
        if (previewTools.includes(this.selectedTool) && this.hoverPosition) {
            return true;
        }
        // Also preserve during active dragging
        if (this.isDraggingBuild && this.previewPath && this.previewPath.length > 0) {
            return true;
        }
        return false;
    }

    // Redraw network with hover preview overlay
    drawWithHoverPreview() {
        if (!this.cachedNetwork) return;
        this.renderer.drawNetwork(this.cachedNetwork);

        if (this.hoverPosition) {
            // Get track bits for proper signal positioning in preview
            let trackBits = null;
            if (this.selectedTool === 'signal') {
                const existingTrack = this.cachedNetwork?.tracks?.find(
                    t => t.x === this.hoverPosition.x && t.y === this.hoverPosition.y
                );
                trackBits = existingTrack?.track_bits || null;
            }
            
            this.renderer.drawHoverPreview(
                this.hoverPosition,
                this.selectedTool,
                this.hoverValid,
                this.previewSignalDirection,  // Pass the preview direction for signal tool
                trackBits  // Pass track bits for proper signal positioning
            );
        }
    }

    async handleRightClick(gridPos) {
        // Check if we're in path tool mode - cancel path creation
        if (this.selectedTool === 'path' && this.pathToolState?.active) {
            this.resetRouteToolState();
            window.showToast('Route creation cancelled', 'info', 3000);
            return;
        }

        // Check if we're in signal tool mode - cycle preview direction
        if (this.selectedTool === 'signal' && this.hoverPosition &&
            this.hoverPosition.x === gridPos.x && this.hoverPosition.y === gridPos.y && this.hoverValid) {
            // Use cached valid directions from API validation
            const validDirections = this.validSignalDirections || [];
            if (validDirections.length > 1) {
                // Cycle to next direction
                this.previewSignalDirectionIndex = (this.previewSignalDirectionIndex + 1) % validDirections.length;
                this.previewSignalDirection = validDirections[this.previewSignalDirectionIndex];
                // Redraw with new preview direction
                this.drawWithHoverPreview();
                return;
            }
        }

        // Get track at position
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (track.error) return;

            // Check if track has switch points (uses is_switch from simplified global state system)
            if (track.is_switch) {
                // Toggle global switch state between MAIN (0) and DIVERGE (1)
                // Backend validates lock status and rejects if switch is locked by a route
                const newState = track.switch_state === 0 ? 1 : 0;
                try {
                    await this.api.updateTrack(gridPos.x, gridPos.y, {
                        switch_state: newState
                    });
                } catch (switchErr) {
                    // Parse error message from API response
                    const errMsg = this._parseApiError(switchErr, 'Cannot toggle switch');
                    window.showToast(errMsg, 'error', 5000);
                }
                
                await window.app.refreshNetwork();
                // Refresh properties panel to show updated switch state
                await this.selectElement(gridPos);
            }
            else if (track.block_section_id) {
                // Right-click cycles block state: FREE → RESERVED → OCCUPIED → CAUTION → FREE
                // Backend validates route reservations and rejects if block is locked by a route
                try {
                    await this.api.cycleBlockState(track.block_section_id);
                } catch (blockErr) {
                    // Parse error message from API response
                    const errMsg = this._parseApiError(blockErr, 'Cannot change block state');
                    window.showToast(errMsg, 'error', 5000);
                }
                await window.app.refreshNetwork();
                // Refresh properties panel to show updated block state
                await this.selectElement(gridPos);
            }
        } catch (err) {
            console.error('Right-click action failed:', err);
        }
    }

    /**
     * Parse API error message from exception.
     * Extracts the 'error' field from JSON response if available.
     */
    _parseApiError(err, fallbackPrefix) {
        try {
            // Error message format: "HTTP 409: {"error": "...", ...}"
            const msg = err.message || String(err);
            const jsonMatch = msg.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                const parsed = JSON.parse(jsonMatch[0]);
                if (parsed.error) {
                    return `${fallbackPrefix}: ${parsed.error}`;
                }
            }
            return `${fallbackPrefix}: ${msg}`;
        } catch {
            return `${fallbackPrefix}: ${err.message || String(err)}`;
        }
    }

    async selectElement(gridPos) {
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (!track.error) {
                // Handle signals - fetch full signal data for each signal
                if (track.signals && track.signals.length > 0) {
                     // We already have signal objects in track.signals, but let's refresh them from API to be safe
                     // or just trust what getTrack returned.
                     // The signals array contains {signal_id, direction, state, etc.}
                } else if (track.signal_ids && track.signal_ids.length > 0) {
                     // If we only have IDs (legacy/alt format), fetch them
                     track.signals = [];
                     for (const signalId of track.signal_ids) {
                         const signal = await this.api.getSignal(signalId);
                         if (signal && !signal.error) {
                             track.signals.push(signal);
                         }
                     }
                }

                // If track has a signal state but no signals array (legacy single signal)
                if (track.signal_state && (!track.signals || track.signals.length === 0)) {
                    // Try to fetch signal at location
                    const signal = await this.api.getSignalAt(gridPos.x, gridPos.y);
                    if (signal && !signal.error) {
                         track.signals = [signal];
                    }
                }

                // Refresh blocks list to ensure dropdowns are up-to-date
                if (track.signals && track.signals.length > 0) {
                    await this.refreshBlocksList();
                }

                this.selectedElement = track;
                this.updatePropertiesPanel(track);

                // Update secondary editor based on new tile's content
                this.updateSecondaryEditorForTile(track);
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

                // Close secondary editor for empty tile
                this.updateSecondaryEditorForTile(this.selectedElement);
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

            // Close secondary editor for error/empty tile
            this.updateSecondaryEditorForTile(this.selectedElement);
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
        
        // Format binary with highlighted 1s
        const binaryStr = trackBits.toString(2).padStart(6, '0');
        const binaryHTML = binaryStr.split('').map(bit => 
            bit === '1' ? `<span style="color: #00d4ff;">${bit}</span>` : bit
        ).join('');
        
        // Build the track bits editor canvas
        const editorId = `track-editor-${Date.now()}`;
        
        panel.innerHTML = `
            <h3>Properties</h3>
            
            <!-- Compact Header: Position and Type on one line with virtual badge -->
            <div class="compact-header">
                <span>(${track.x}, ${track.y}) - ${track.isVirtual ? 'Empty' : track.type}</span>
                ${track.isVirtual ? '<span class="badge badge-new">NEW</span>' : ''}
            </div>
            
            <!-- Track Bits Display (Primary) - Larger font, compact layout -->
            <div class="info-box-track">
                <label class="text-track font-bold block mb-4">Track Bits</label>
                <div class="text-2xl font-bold mb-2 text-track" style="font-family: monospace;">${trackBits} <span class="text-lg text-muted">(0b${binaryHTML})</span></div>
                <div class="text-muted text-base mb-8">
                    ${shapeDesc} • ${numSegments} segment${numSegments !== 1 ? 's' : ''} • ${activeSegments.length > 0 ? activeSegments.join(', ') : 'No segments'} ${edges.length > 0 ? '• Edges: ' + edges.join(', ') : ''}
                </div>
                ${isForbidden ?
                    '<div class="text-xs text-danger mt-8 font-bold">⚠ FORBIDDEN CONFIGURATION</div>'
                    : ''}
            </div>

            <!-- Combined Bit Editor (33%) and Visual Editor (67%) Side-by-Side -->
            <div class="flex gap-8" style="margin: 8px 0;">
                <!-- Bit Editor (Left - 33%) -->
                <div id="bit-editor" class="bg-section p-8 rounded" style="flex: 0 0 33%;">
                    <h4 class="text-track text-xs mb-8" style="margin: 0 0 8px 0;">Bit Editor</h4>
                    <div class="flex flex-col gap-4">
                        ${renderBitToggle(1, 'NS', trackBits)}
                        ${renderBitToggle(2, 'EW', trackBits)}
                        ${renderBitToggle(4, 'NE', trackBits)}
                        ${renderBitToggle(8, 'NW', trackBits)}
                        ${renderBitToggle(16, 'SE', trackBits)}
                        ${renderBitToggle(32, 'SW', trackBits)}
                    </div>
                    <div class="bit-info bg-hover rounded-sm text-tiny" style="margin: 8px 0 0 0; padding: 4px;">
                        <p style="margin: 0;"><span id="bit-total" class="font-bold text-track">${trackBits}</span> <span class="text-muted">(0b${binaryHTML})</span></p>
                        <p class="bit-warning font-bold text-danger mt-2" style="font-size: 8px; display: ${isForbidden ? 'block' : 'none'};">
                            ⚠ Forbidden
                        </p>
                    </div>
                    <button id="apply-bits" class="btn btn-primary btn-full text-xxs mt-6">Apply</button>
                </div>
                
                <!-- Visual Editor (Right - 67%) -->
                <div class="visual-editor bg-section p-8 rounded" style="flex: 1;">
                    <label class="text-track font-bold text-xs mb-6 block">Visual Editor</label>
                    <canvas id="${editorId}" width="200" height="200" class="block w-full rounded" style="height: auto; cursor: pointer; border: 1px solid #444; background: #0a0a14;"></canvas>
                    <div class="helper-text text-center">Click segments to toggle</div>
                </div>
            </div>
            
            ${track.switch_points && Object.keys(track.switch_points).length > 0 ? `
            <div class="info-box-signal">
                <label class="text-info font-bold">Switch State:</label>
                ${track.is_diagonal_only_switch ? `
                <div class="info-box-warning text-xxs text-warning" style="margin: 4px 0; padding: 4px 8px;">
                    ⟳ Diagonal-only switch (uses clockwise ordering)
                </div>
                ` : ''}
                <div class="p-8 rounded ${track.switch_state === 0 ? 'border-success' : 'border-warning'}" style="margin: 6px 0; background: ${track.switch_state === 0 ? '#1a3a2a' : '#3a3a1a'};">
                    <span class="text-lg font-bold" style="color: ${track.switch_state === 0 ? '#04d361' : '#ffd93d'};">
                        ${track.switch_state === 0 ? 'MAIN (0)' : 'DIVERGE (1)'}
                        ${track.is_locked || track.locked_by_path ? ' 🔒' : ''}
                    </span>
                    <div class="text-xxs text-secondary mt-4">
                        ${track.is_diagonal_only_switch
                            ? (track.switch_state === 0 ? 'Clockwise first exit' : 'Clockwise second exit')
                            : (track.switch_state === 0 ? 'Long paths active: NS, EW' : 'Short paths active: NE, NW, SE, SW')}
                    </div>
                </div>
                ${track.is_locked || track.locked_by_path ? `
                <div class="info-box-warning text-xxs text-warning" style="margin: 4px 0; padding: 4px 8px;">
                    🔒 Locked by path: ${track.locked_by_path || 'Unknown'}
                </div>
                ` : `
                <div class="helper-text text-dim mt-4">Right-click to toggle switch</div>
                `}
            </div>
            <div class="info-box-signal">
                <label class="text-info font-bold">Switch Points:</label>
                <pre class="text-xxs mt-4 mb-0" style="color: #ccc;">${
                    Object.entries(track.switch_points).map(([entry, exits]) => 
                        `${entry} -> [${exits.join(', ')}]`
                    ).join('\n')
                }</pre>
            </div>
            ` : ''}
            ${track.signals && track.signals.length > 0 ? `
            <div class="property-group-header">Signals</div>
            ${track.signals.map(signal => `
            <div class="property-item-row" style="margin-bottom: 8px;">
                <div class="signal-summary-row">
                    <div class="signal-status-dot" style="background-color: ${
                        signal.signal_state === 'green' || signal.signal_state === 'GREEN' ? '#04d361' :
                        (signal.signal_state === 'yellow' || signal.signal_state === 'YELLOW' ? '#ffd93d' : '#e83f5b')
                    }"></div>
                    <div class="signal-info">
                        <span class="signal-id">${signal.signal_id}</span>
                        <span class="signal-dir">→${signal.detection_direction}</span>
                    </div>
                    <button class="edit-btn edit-signal-btn" data-signal-id="${signal.signal_id}">Edit</button>
                </div>
            </div>
            `).join('')}
            ` : ''}

            <!-- Block Summary (Compact view with Edit button) -->
            ${track.block_section_id ? (() => {
                const block = this.cachedBlocks?.find(b => b.block_id === track.block_section_id);
                const blockColor = this.getBlockColor(track.block_section_id);
                const stateColor = block?.state === 'free' ? '#04d361' :
                                   block?.state === 'reserved' ? '#ffd93d' :
                                   block?.state === 'occupied' ? '#e83f5b' :
                                   block?.state === 'caution' ? '#ff9500' : '#888';
                return `
                <div class="property-group-header">Block</div>
                <div class="property-item-row">
                    <div class="block-summary-row" style="border-left: 3px solid ${blockColor}">
                        <div class="block-info">
                            <span class="block-id">${track.block_section_id}</span>
                            <span class="block-state" style="color: ${stateColor}">${block?.state || 'UNKNOWN'}</span>
                        </div>
                        <button class="edit-btn edit-block-btn" data-block-id="${track.block_section_id}">Edit</button>
                    </div>
                </div>
                `;
            })() : ''}

            <!-- Station Info Display -->
            ${track.station_id ? (() => {
                const station = this.cachedNetwork?.stations?.find(s => s.station_id === track.station_id);
                return `
                <div class="property-group-header">Station</div>
                <div class="property-item-row">
                    <div class="station-summary-row">
                        <div class="station-info">
                            <span class="station-id">${track.station_id}</span>
                            ${station?.name ? `<span class="station-name">${station.name}</span>` : ''}
                        </div>
                        <button class="edit-btn edit-station-btn" data-station-id="${track.station_id}">Edit</button>
                    </div>
                </div>
                `;
            })() : ''}
            ${track.occupied_by ? `
            <div class="property-item">
                <label>Occupied By:</label>
                <span class="text-danger">${track.occupied_by}</span>
            </div>
            ` : ''}
            ${track.dead_end_ports && track.dead_end_ports.length > 0 ? `
            <div class="property-item">
                <label class="property-label">Dead Ends</label>
                <div class="value-display text-warning">${track.dead_end_ports.join(', ')}</div>
            </div>
            ` : ''}
            
            <!-- View Full JSON Button -->
            <button id="view-full-json-btn" class="btn btn-primary btn-full text-sm mt-12">
                📄 View Full JSON
            </button>
        `;
        
        // Initialize track bits editor after panel is rendered
        setTimeout(() => this.initBitEditor(track), 0);
        setTimeout(() => this.initVisualTrackEditor(editorId, track), 0);
        setTimeout(() => this.initSignalEditor(track), 0);
        setTimeout(() => this.initBlockEditor(track), 0);
        setTimeout(() => this.initStationButtonHandlers(), 0);
        setTimeout(() => this.initViewFullJSONButton(track), 0);
    }

    // Initialize View Full JSON button handler
    initViewFullJSONButton(track) {
        const viewJsonBtn = document.getElementById('view-full-json-btn');
        if (viewJsonBtn) {
            viewJsonBtn.onclick = () => {
                this.showFullJSONViewer(track, `📄 JSON: (${track.x}, ${track.y})`);
            };
        }
    }

    // Initialize station edit button handler
    initStationButtonHandlers() {
        const editStationBtns = document.querySelectorAll('.edit-station-btn');
        editStationBtns.forEach(btn => {
            btn.onclick = () => {
                const stationId = btn.dataset.stationId;
                if (stationId) {
                    this.showStationEditor(stationId);
                }
            };
        });
    }

    initSignalEditor(track) {
        // Updated signal editor logic - show ALL signals on the tile
        const editSignalBtns = document.querySelectorAll('.edit-signal-btn');
        editSignalBtns.forEach(btn => {
            btn.onclick = () => {
                // Show all signals on this tile, not just one
                this.showSignalsEditorForTile(track);
            };
        });
    }

    async showSignalEditor(signalId) {
        // Track this editor for auto-refresh
        this._currentEditorType = 'signal';
        this._currentEditorId = signalId;

        // Fetch full signal details
        const signal = await this.api.getSignal(signalId);
        if (!signal || signal.error) {
            window.showToast('Signal not found', 'error', 5000);
            return;
        }

        const content = `
            <div class="signal-properties" style="background: #1e2d23; padding: 16px; border-radius: 8px; margin: 12px 0; border-left: 4px solid ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};">

                <!-- Header with Title and ID -->
                <div style="margin-bottom: 16px; border-bottom: 1px solid #3a4a3e; padding-bottom: 12px;">
                    <div style="font-size: 14px; color: #00ff88; font-weight: bold; margin-bottom: 8px;">
                        🚦 Signal Properties
                    </div>
                    <div style="font-family: monospace; font-size: 13px; color: #00d4ff; background: #0d1a12; padding: 6px 10px; border-radius: 4px;">
                        ${signal.signal_id}
                    </div>
                </div>

                <!-- Current State Display -->
                <div style="margin-bottom: 16px;">
                    <div style="font-size: 12px; color: #aaa; margin-bottom: 8px;">Current State</div>
                    <div style="display: flex; align-items: center; gap: 12px; background: #0d1a12; padding: 10px; border-radius: 6px;">
                        <span style="display: inline-block; width: 20px; height: 20px; border-radius: 50%; background: ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')}; box-shadow: 0 0 8px ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};"></span>
                        <span style="font-size: 16px; font-weight: bold; color: ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};">
                            ${(signal.state || signal.signal_state || 'GREEN').toUpperCase()}
                        </span>
                    </div>
                </div>

                <!-- Signal Name Input -->
                <div style="margin-bottom: 16px;">
                    <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Signal Name</label>
                    <input type="text" id="signal-name-input" value="${signal.name || ''}" placeholder="Enter signal name..."
                           style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                </div>

                <!-- Direction Display (Read-only) -->
                <div style="margin-bottom: 16px;">
                    <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Detection Direction (Train travels →)</label>
                    <div style="display: flex; align-items: center; gap: 8px; background: #0d1a12; padding: 10px 12px; border: 1px solid #3a4a3e; border-radius: 6px;">
                        <span style="font-size: 13px; color: ${signal.detection_direction ? '#fff' : '#f55'}; font-weight: bold;">${signal.detection_direction || '???'}</span>
                        <span style="font-size: 10px; color: #666; margin-left: auto;">(Set at creation)</span>
                    </div>
                </div>

                <!-- Block to Monitor Dropdown -->
                <div style="margin-bottom: 16px;">
                    <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Block to Monitor</label>
                    <select id="signal-block-select" style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid ${signal.monitored_block_id ? '#3a4a3e' : '#ff9500'}; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                        <option value="">-- None (Manual Control) --</option>
                        <option value="${signal.monitored_block_id}" selected>${signal.monitored_block_id} (Loading...)</option>
                    </select>
                    <div id="block-select-hint" style="font-size: 10px; color: #666; margin-top: 6px;">
                        Signal shows RED when monitored block is occupied
                    </div>
                </div>

                <!-- Save Button -->
                <button id="save-signal-btn" class="btn btn-primary btn-full text-base">
                    Save Signal Properties
                </button>
            </div>
        `;

        this.showSecondarySidebar('Signal Editor', content);

        // Populate reachable blocks
        try {
            const result = await this.api.getReachableBlocks(signalId);
            const reachableBlockIds = result.reachable_blocks || [];

            // Build reachable blocks list - include blocks from cache AND fetch any missing ones
            const reachableBlocks = [];
            for (const blockId of reachableBlockIds) {
                let block = (this.cachedBlocks || []).find(b => b.block_id === blockId);
                if (!block) {
                    try {
                        block = await this.api.getBlock(blockId);
                        if (block && !block.error) {
                            this.cachedBlocks = this.cachedBlocks || [];
                            this.cachedBlocks.push(block);
                        }
                    } catch (err) {
                        block = { block_id: blockId, name: null };
                    }
                }
                if (block) reachableBlocks.push(block);
            }

            const blockSelect = document.getElementById('signal-block-select');
            if (blockSelect) {
                const currentValue = signal.monitored_block_id;
                blockSelect.innerHTML = `
                    <option value="">-- None (Manual Control) --</option>
                    ${reachableBlocks.map(b => `
                        <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                            ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                        </option>
                    `).join('')}
                `;

                const hintDiv = document.getElementById('block-select-hint');
                if (hintDiv) {
                     if (reachableBlocks.length === 0) {
                        hintDiv.innerHTML = `<span style="color: #ff6b6b;">No reachable blocks found. Check track connectivity.</span>`;
                     } else {
                        hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span style="color: #888;">${reachableBlocks.length} reachable block(s)</span>`;
                     }
                }
            }
        } catch (err) {
            console.warn('Failed to fetch reachable blocks:', err);
        }

        // Setup save handler
        const saveBtn = document.getElementById('save-signal-btn');
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const nameInput = document.getElementById('signal-name-input');
                const blockSelect = document.getElementById('signal-block-select');

                const updates = {};
                if (nameInput) updates.name = nameInput.value.trim() || null;
                if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

                try {
                    await this.api.updateSignal(signalId, updates);
                    window.showToast('Signal updated successfully', 'success', 'error', 5000);
                    await window.app.refreshNetwork();

                    // Refresh main panel if we are still on the same tile
                    if (this.selectedElement) {
                        await this.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save signal properties:', err);
                    window.showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
                }
            };
        }
    }

    // Initialize block summary edit button handler
    async initBlockEditor(track) {
        if (!track.block_section_id) return;

        // Find and attach click handler to Edit button
        const editBtn = document.querySelector(`.edit-block-btn[data-block-id="${track.block_section_id}"]`);
        if (editBtn) {
            editBtn.onclick = () => {
                this.showBlockEditor(track.block_section_id);
            };
        }
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
                    } else {
                        // Update existing track via API
                        const result = await window.api.updateTrack(track.x, track.y, { track_bits: newBits });
                    }

                    // Refresh network and properties panel
                    await window.app.refreshNetwork();
                    await this.selectElement({x: track.x, y: track.y});
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
                await this.selectElement({x: track.x, y: track.y});
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

        // Re-validate hover state if we have an active hover position
        // This ensures the hover preview reflects the updated network state
        if (this.hoverPosition && ['signal', 'station'].includes(this.selectedTool)) {
            // Re-run validation with the new network data
            this.updateHoverPreview(this.hoverPosition);
        }
    }

    // === Route Tool Methods ===

    /**
     * Enter path tool mode - prepare for path creation
     */
    enterRouteToolMode() {
        this.pathToolState = {
            active: false,  // Not active until first click
            startBlockId: null,
            startPosition: null,
            reachableEndpoints: [],
            reachablePositions: new Set(),
            priority: 5,
            trainId: null
        };
        document.getElementById('status-state').textContent = 'Route: Click start block';
    }

    /**
     * Exit path tool mode - cleanup
     */
    exitRouteToolMode() {
        this.resetRouteToolState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    /**
     * Reset route tool state without exiting the tool
     */
    resetRouteToolState() {
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,
            reachableEndpoints: [],
            reachablePositions: new Set(),
            priority: 5,
            trainId: null
        };
        // Clear renderer highlights
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.renderer.clearRoutePreview) {
            this.renderer.clearRoutePreview();
        }
        // Hide secondary sidebar if showing route setup
        if (this._currentEditorType === 'path-setup') {
            this.hideSecondarySidebar();
        }
        // Redraw
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }
        document.getElementById('status-state').textContent = 'Route: Click start block';
    }

    /**
     * Handle click in path tool mode
     */
    async handleRouteToolClick(gridPos) {
        // Find the track at this position
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);
        
        if (!this.pathToolState.active) {
            // First click - select start block
            await this.handleRouteFirstClick(gridPos, track);
        } else {
            // Second click - select end block
            await this.handleRouteSecondClick(gridPos, track);
        }
    }

    /**
     * Handle first click in route tool - select start block
     */
    async handleRouteFirstClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('Must start path from a block', 'warning', 3000);
            return;
        }

        // Store start info
        this.pathToolState.startBlockId = blockId;
        this.pathToolState.startPosition = { x: gridPos.x, y: gridPos.y };
        this.pathToolState.active = true;

        // Check if block has a train
        const block = this.cachedNetwork?.blocks?.[blockId];
        if (block?.occupied_by) {
            this.pathToolState.trainId = block.occupied_by;

            // Get train priority as default
            const trains = this.cachedNetwork?.trains || [];
            const train = trains.find(t => t.train_id === block.occupied_by);
            if (train?.priority) {
                this.pathToolState.priority = train.priority;
            }
        }

        // Fetch reachable endpoints
        try {
            document.getElementById('status-state').textContent = 'Route: Loading destinations...';
            const result = await this.api.getReachableEndpoints(blockId);
            this.pathToolState.reachableEndpoints = result.reachable_endpoints || [];

            // Build set of reachable tile positions for rendering
            this.buildReachablePositionsSet();

            // Update status
            const count = this.pathToolState.reachableEndpoints.length;
            document.getElementById('status-state').textContent = `Route: Click destination (${count} reachable)`;

            // Set renderer highlights
            if (this.renderer.setReachableHighlights) {
                this.renderer.setReachableHighlights(this.pathToolState.reachablePositions);
            }

            // Show priority selector panel
            this.showRoutePrioritySelector();

            // Redraw to show highlights
            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            }
        } catch (err) {
            console.error('[Editor] Failed to get reachable destinations:', err);
            window.showToast('Failed to get reachable destinations: ' + err.message, 'error', 5000);
            this.resetRouteToolState();
        }
    }

    /**
     * Build set of tile positions for reachable blocks (for rendering highlights)
     */
    buildReachablePositionsSet() {
        const positions = new Set();
        const tracks = this.cachedNetwork?.tracks || [];

        for (const endpoint of this.pathToolState.reachableEndpoints) {
            // Find all tracks belonging to this block
            for (const track of tracks) {
                if (track.block_section_id === endpoint) {
                    positions.add(`${track.x},${track.y}`);
                }
            }
        }

        this.pathToolState.reachablePositions = positions;
    }

    /**
     * Handle second click in route tool - select end block and create route
     */
    async handleRouteSecondClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const endBlockId = track.block_section_id;
        if (!endBlockId) {
            window.showToast('Must end path on a block', 'warning', 3000);
            return;
        }

        // Check if endpoint is reachable
        if (!this.pathToolState.reachableEndpoints.includes(endBlockId)) {
            window.showToast('Cannot reach this block from start', 'warning', 3000);
            return;
        }

        // Create route
        try {
            document.getElementById('status-state').textContent = 'Route: Creating...';
            
            const options = {
                priority: this.pathToolState.priority
            };
            
            // Only include train_id if we have one
            if (this.pathToolState.trainId) {
                options.train_id = this.pathToolState.trainId;
            }

            const result = await this.api.createRoute(
                this.pathToolState.startBlockId,
                endBlockId,
                options
            );

            if (result.success) {
                window.showToast(`Route ${result.path.path_id} created`, 'success', 5000);
                await window.app.refreshNetwork();
                this.resetRouteToolState();
            } else {
                window.showToast(`Route failed: ${result.error}`, 'error', 5000);

                // Flash conflict tiles if any
                if (result.conflict_tiles && this.renderer.flashConflictTiles) {
                    this.renderer.flashConflictTiles(result.conflict_tiles, 2000);
                }
            }
        } catch (err) {
            console.error('[Editor] Failed to create path:', err);
            window.showToast('Failed to create path: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Show priority selector panel during path creation
     */
    showRoutePrioritySelector() {
        this._currentEditorType = 'path-setup';
        this._currentEditorId = null;

        const trainInfo = this.pathToolState.trainId
            ? renderReadOnlyValue('Train', `🚂 ${this.pathToolState.trainId}`, 'text-info font-bold')
            : '<div class="text-muted text-sm" style="padding: 8px 0;">No train at start block</div>';

        const content = `
            <div class="path-setup-panel">
                ${renderHeaderId(this.pathToolState.startBlockId, '#00d4ff')}
                
                <div class="property-item">
                    <label class="property-label">Priority (1-10)</label>
                    <div class="flex items-center gap-12">
                        <input type="range" id="path-priority-slider" 
                               min="1" max="10" value="${this.pathToolState.priority}"
                               class="property-input" style="flex: 1;">
                        <span id="path-priority-value" class="text-lg font-bold text-track" style="min-width: 30px; text-align: center;">
                            ${this.pathToolState.priority}
                        </span>
                    </div>
                    <div class="text-xxs text-muted" style="margin-top: 4px;">
                        5 = Normal, 10 = Critical
                    </div>
                </div>
                
                ${trainInfo}
                
                <div class="info-box text-sm" style="margin-top: 16px;">
                    Click on an <span style="color: #ff9500;">orange</span> destination block to create path.
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

        this.showSecondarySidebar('🛤️ Create Route', content);
        
        // Add compact class for minimal width
        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.add('compact');
        }

        // Wire up slider
        setTimeout(() => {
            const slider = document.getElementById('path-priority-slider');
            const valueDisplay = document.getElementById('path-priority-value');
            if (slider && valueDisplay) {
                slider.addEventListener('input', (e) => {
                    this.pathToolState.priority = parseInt(e.target.value);
                    valueDisplay.textContent = e.target.value;
                });
            }
        }, 0);
    }

    // === Routes Panel Methods ===

    /**
     * Show list of active routes in secondary sidebar
     */
    async showPathsPanel() {
        this._currentEditorType = 'paths-list';
        this._currentEditorId = null;

        try {
            const result = await this.api.getPaths();
            this.cachedPaths = result.paths || [];
        } catch (err) {
            console.error('[Editor] Failed to load paths:', err);
            window.showToast('Failed to load paths: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="paths-panel">
                <button id="refresh-paths-btn" class="btn btn-secondary btn-full text-sm mb-8">
                    🔄 Refresh Routes
                </button>
        `;

        if (this.cachedPaths.length === 0) {
            content += '<div class="text-muted text-center" style="padding: 32px 16px;">No active paths</div>';
        } else {
            content += '<div class="paths-list">';
            for (const path of this.cachedPaths) {
                content += this.renderPathListItem(path);
            }
            content += '</div>';
        }

        content += '</div>';

        this.showSecondarySidebar('📋 Active Routes', content);
        
        // Initialize handlers after content is rendered
        setTimeout(() => this.initRoutesPanelHandlers(), 0);
    }

    /**
     * Render a single route list item
     */
    renderPathListItem(path) {
        const statusColor = this.getPathStatusColor(path.status);
        const progress = path.block_sequence?.length > 0
            ? Math.round((path.current_block_index / path.block_sequence.length) * 100)
            : 0;

        return `
            <div class="path-item" data-path-id="${path.path_id}">
                <div class="path-header">
                    <span class="path-status" style="color: ${statusColor}">
                        ● ${path.status.toUpperCase()}
                    </span>
                    <span class="path-priority">P${path.priority}</span>
                </div>
                <div class="path-id text-sm value-display-mono">${path.path_id}</div>
                <div class="path-endpoints text-xs text-muted">
                    ${path.start_block_id} → ${path.end_block_id}
                </div>
                ${path.train_id ? `<div class="path-train text-xs">🚂 ${path.train_id}</div>` : ''}
                ${path.status === 'active' ? `
                    <div class="progress-container">
                        <div class="progress-bar" style="width: ${progress}%"></div>
                        <span class="progress-text">${progress}%</span>
                    </div>
                ` : ''}
                <div class="path-actions">
                    <button class="btn btn-sm btn-info view-path-btn" data-path-id="${path.path_id}">View</button>
                    <button class="btn btn-sm btn-danger cancel-path-btn" data-path-id="${path.path_id}">Cancel</button>
                </div>
            </div>
        `;
    }

    /**
     * Get color for route status
     */
    getPathStatusColor(status) {
        const colors = {
            'pending': '#888',
            'setting': '#ffd93d',
            'set': '#04d361',
            'active': '#00ff88',
            'clearing': '#ff9500',
            'completed': '#666',
            'cancelled': '#e83f5b'
        };
        return colors[status] || '#888';
    }

    /**
     * Initialize handlers for routes panel
     */
    initRoutesPanelHandlers() {
        // Refresh button
        const refreshBtn = document.getElementById('refresh-paths-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                await this.showPathsPanel();
            };
        }

        // View buttons
        const viewBtns = document.querySelectorAll('.view-path-btn');
        viewBtns.forEach(btn => {
            btn.onclick = async () => {
                const pathId = btn.dataset.pathId;
                await this.showPathEditor(pathId);
            };
        });

        // Cancel buttons
        const cancelBtns = document.querySelectorAll('.cancel-path-btn');
        cancelBtns.forEach(btn => {
            btn.onclick = async () => {
                const pathId = btn.dataset.pathId;
                if (!confirm(`Cancel path "${pathId}"?`)) return;
                
                try {
                    const result = await this.api.cancelRoute(pathId);
                    if (result.success) {
                        window.showToast(`Route ${pathId} cancelled`, 'success', 5000);
                        await window.app.refreshNetwork();
                        await this.showPathsPanel();
                    } else {
                        window.showToast(`Failed to cancel: ${result.message || result.error}`, 'error', 5000);
                    }
                } catch (err) {
                    console.error('[Editor] Failed to cancel path:', err);
                    window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    /**
     * Show add train dialog
     */
    showAddTrainDialog() {
        const content = `
            <div class="add-train-panel">
                <div class="form-group">
                    <label for="train-id-input">Train ID</label>
                    <input type="text" id="train-id-input" placeholder="e.g., TRAIN_01" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                </div>
                <div class="form-group">
                    <label for="train-position-x">Position X</label>
                    <input type="number" id="train-position-x" value="0" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                </div>
                <div class="form-group">
                    <label for="train-position-y">Position Y</label>
                    <input type="number" id="train-position-y" value="0" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                </div>
                <div class="form-group">
                    <label for="train-direction">Direction</label>
                    <select id="train-direction" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                        <option value="NORTH">North</option>
                        <option value="EAST" selected>East</option>
                        <option value="SOUTH">South</option>
                        <option value="WEST">West</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="train-max-speed">Max Speed (km/h)</label>
                    <input type="number" id="train-max-speed" value="100" min="1" max="200" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                </div>
                <div class="form-group">
                    <label for="train-length">Length (units)</label>
                    <input type="number" id="train-length" value="1" min="1" max="10" style="width: 100%; padding: 10px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px;">
                </div>
                <button id="btn-confirm-add-train" class="btn btn-primary btn-full mt-16">🚂 Add Train</button>
            </div>
        `;

        this.showSecondarySidebar('🚂 Add Train', content);

        // Initialize handlers after content is rendered
        setTimeout(() => {
            const addBtn = document.getElementById('btn-confirm-add-train');
            if (addBtn) {
                addBtn.onclick = async () => {
                    const trainId = document.getElementById('train-id-input').value.trim();
                    const posX = parseInt(document.getElementById('train-position-x').value) || 0;
                    const posY = parseInt(document.getElementById('train-position-y').value) || 0;
                    const direction = document.getElementById('train-direction').value;
                    const maxSpeed = parseInt(document.getElementById('train-max-speed').value) || 100;
                    const length = parseFloat(document.getElementById('train-length').value) || 1;

                    if (!trainId) {
                        window.showToast('Please enter a train ID', 'error', 3000);
                        return;
                    }

                    try {
                        await window.api.createTrain({
                            id: trainId,
                            position: { x: posX, y: posY },
                            direction: direction,
                            max_speed: maxSpeed,
                            length: length
                        });
                        window.showToast(`Train "${trainId}" created successfully`, 'success', 3000);
                        this.hideSecondarySidebar();
                        window.app.refreshNetwork();
                    } catch (err) {
                        console.error('[Editor] Failed to create train:', err);
                        window.showToast('Failed to create train: ' + err.message, 'error', 5000);
                    }
                };
            }

            // Auto-generate train ID
            const idInput = document.getElementById('train-id-input');
            if (idInput && !idInput.value) {
                idInput.value = `TRAIN_${Date.now().toString().slice(-4)}`;
            }
        }, 0);
    }

    /**
     * Show trains management panel
     */
    async showTrainsPanel() {
        this._currentEditorType = 'trains-list';
        this._currentEditorId = null;

        let trains = [];
        try {
            const result = await this.api.getTrains();
            trains = result.trains || [];
        } catch (err) {
            console.error('[Editor] Failed to load trains:', err);
            window.showToast('Failed to load trains: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="trains-panel">
                <button id="refresh-trains-btn" class="btn btn-secondary btn-full text-sm mb-8">
                    🔄 Refresh Trains
                </button>
                <button id="add-new-train-btn" class="btn btn-primary btn-full text-sm mb-16">
                    🚂 Add New Train
                </button>
        `;

        if (trains.length === 0) {
            content += '<div class="text-muted text-center" style="padding: 32px 16px;">No trains in network</div>';
        } else {
            content += '<div class="trains-list">';
            for (const train of trains) {
                content += this.renderTrainListItem(train);
            }
            content += '</div>';
        }

        content += '</div>';

        this.showSecondarySidebar('🚂 Trains', content);

        // Initialize handlers after content is rendered
        setTimeout(() => this.initTrainsPanelHandlers(), 0);
    }

    /**
     * Render a single train list item
     */
    renderTrainListItem(train) {
        const state = train.state || 'off';
        const stateColor = state === 'running_moving' ? '#04d361' : (state === 'off' ? '#e83f5b' : '#ffd93d');
        const position = train.position || train.current_pos || { x: 0, y: 0 };

        return `
            <div class="train-item" data-train-id="${train.id}">
                <div class="train-header">
                    <span class="train-status" style="color: ${stateColor}">
                        ● ${state.toUpperCase().replace('_', ' ')}
                    </span>
                </div>
                <div class="train-id text-sm value-display-mono">${train.id}</div>
                <div class="train-position text-xs text-muted">
                    📍 (${position.x}, ${position.y})
                </div>
                <div class="train-speed text-xs">
                    🚄 ${(train.current_speed || 0).toFixed(1)} / ${train.max_speed || 100} km/h
                </div>
                ${train.facing_direction ? `
                <div class="train-direction text-xs">
                    Facing: ${train.facing_direction}
                </div>
                ` : ''}
                <div class="train-actions mt-8">
                    <button class="btn btn-sm btn-info select-train-btn" data-train-id="${train.id}">Select</button>
                    <button class="btn btn-sm btn-danger delete-train-btn" data-train-id="${train.id}">Delete</button>
                </div>
            </div>
        `;
    }

    /**
     * Initialize handlers for trains panel
     */
    initTrainsPanelHandlers() {
        // Refresh button
        const refreshBtn = document.getElementById('refresh-trains-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                await this.showTrainsPanel();
            };
        }

        // Add new train button
        const addBtn = document.getElementById('add-new-train-btn');
        if (addBtn) {
            addBtn.onclick = () => {
                this.showAddTrainDialog();
            };
        }

        // Select buttons
        const selectBtns = document.querySelectorAll('.select-train-btn');
        selectBtns.forEach(btn => {
            btn.onclick = () => {
                const trainId = btn.dataset.trainId;
                window.app.selectTrain(trainId);
                this.hideSecondarySidebar();
            };
        });

        // Delete buttons
        const deleteBtns = document.querySelectorAll('.delete-train-btn');
        deleteBtns.forEach(btn => {
            btn.onclick = async () => {
                const trainId = btn.dataset.trainId;
                if (!confirm(`Delete train "${trainId}"?`)) return;

                try {
                    await window.api.deleteTrain(trainId);
                    window.showToast(`Train "${trainId}" deleted`, 'success', 3000);
                    await this.showTrainsPanel();
                    window.app.refreshNetwork();
                } catch (err) {
                    console.error('[Editor] Failed to delete train:', err);
                    window.showToast('Failed to delete train: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    /**
     * Show single route editor/details
     */
    async showPathEditor(pathId) {
        this._currentEditorType = 'path';
        this._currentEditorId = pathId;

        let path;
        try {
            path = await this.api.getPath(pathId);
            if (path.error) throw new Error(path.error);
        } catch (err) {
            window.showToast('Path not found', 'error', 5000);
            return;
        }

        const statusColor = this.getPathStatusColor(path.status);
        const progress = path.block_sequence?.length > 0
            ? Math.round((path.current_block_index / path.block_sequence.length) * 100)
            : 0;

        // Build blocks list HTML
        const blocksListHtml = (path.block_sequence || []).map((blockId, i) => {
            const isCurrent = i === path.current_block_index;
            const isCleared = path.blocks_cleared?.includes(blockId);
            let cssClass = 'text-muted';
            if (isCurrent) cssClass = 'text-success font-bold';
            else if (isCleared) cssClass = 'text-muted';
            
            return `<div class="block-item text-xs ${cssClass}">
                ${isCurrent ? '▶ ' : '  '}${blockId}${isCleared ? ' ✓' : ''}
            </div>`;
        }).join('');

        const content = `
            <div class="path-editor-panel" data-path-id="${pathId}">
                ${renderHeaderId(path.path_id, statusColor)}
                
                ${renderPropertyItem('Status', `
                    <div class="value-display flex items-center gap-8">
                        <span class="status-dot" style="background: ${statusColor}; width: 12px; height: 12px; border-radius: 50%; display: inline-block;"></span>
                        <span class="font-bold" style="color: ${statusColor}">${path.status.toUpperCase()}</span>
                    </div>
                `)}
                
                ${renderReadOnlyValue('Priority', `${path.priority} / 10`, 'font-bold')}
                
                ${path.train_id
                    ? renderReadOnlyValue('Train', `🚂 ${path.train_id}`, 'text-info font-bold')
                    : renderReadOnlyValue('Train', 'None assigned', 'text-muted')
                }
                
                ${renderReadOnlyValue('Start Block', path.start_block_id, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('End Block', path.end_block_id, 'value-display-mono text-sm')}
                
                ${path.status === 'active' ? `
                    <div class="property-item">
                        <label class="property-label">Progress</label>
                        <div class="progress-container">
                            <div class="progress-bar" style="width: ${progress}%"></div>
                            <span class="progress-text">${progress}%</span>
                        </div>
                    </div>
                ` : ''}
                
                <div class="property-item">
                    <label class="property-label">Blocks (${path.block_sequence?.length || 0})</label>
                    <div class="bg-input rounded-lg" style="max-height: 150px; overflow-y: auto; padding: 8px; border: 1px solid #3a3a4e;">
                        ${blocksListHtml || '<div class="text-muted text-xs">No blocks</div>'}
                    </div>
                </div>
                
                ${path.approach_locked_blocks?.length > 0 ? `
                    <div class="info-box-warning text-xs text-warning">
                        ⚠️ ${path.approach_locked_blocks.length} blocks approach-locked
                    </div>
                ` : ''}
                
                <div class="property-item-row" style="margin-top: 16px;">
                    ${(path.status === 'set' || path.status === 'active') ? `
                        <button id="extend-path-btn" class="btn btn-primary text-sm" style="flex: 1;">
                            Extend Route
                        </button>
                    ` : ''}
                    <button id="cancel-path-btn" class="btn btn-danger text-sm" style="flex: 1;">
                        Cancel Route
                    </button>
                </div>
                
                <button id="back-to-paths-btn" class="btn btn-secondary btn-full text-sm" style="margin-top: 8px;">
                    ← Back to Routes List
                </button>
            </div>
        `;

        this.showSecondarySidebar('🛤️ Path Details', content);

        // Initialize handlers
        setTimeout(() => this.initPathEditorHandlers(pathId, path), 0);

        // Highlight path on canvas
        if (this.renderer.highlightPath) {
            this.renderer.highlightPath(path);
        }
    }

    /**
     * Initialize handlers for path editor
     */
    initPathEditorHandlers(pathId, path) {
        // Back button
        const backBtn = document.getElementById('back-to-paths-btn');
        if (backBtn) {
            backBtn.onclick = async () => {
                if (this.renderer.clearPathHighlight) {
                    this.renderer.clearPathHighlight();
                }
                await this.showPathsPanel();
            };
        }

        // Cancel button
        const cancelBtn = document.getElementById('cancel-path-btn');
        if (cancelBtn) {
            cancelBtn.onclick = async () => {
                const hasApproachLocked = path.approach_locked_blocks?.length > 0;
                const msg = hasApproachLocked
                    ? `Cancel path "${pathId}"?\n\nNote: ${path.approach_locked_blocks.length} blocks are approach-locked and will be released when train stops.`
                    : `Cancel path "${pathId}"?`;
                
                if (!confirm(msg)) return;

                try {
                    const result = await this.api.cancelPath(pathId);
                    if (result.success) {
                        window.showToast(`Path ${pathId} cancelled`, 'success', 5000);
                        await window.app.refreshNetwork();
                        if (this.renderer.clearPathHighlight) {
                            this.renderer.clearPathHighlight();
                        }
                        await this.showPathsPanel();
                    } else {
                        window.showToast(`Failed to cancel: ${result.message || result.error}`, 'error', 5000);
                    }
                } catch (err) {
                    console.error('[Editor] Failed to cancel path:', err);
                    window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
                }
            };
        }

        // Extend button
        const extendBtn = document.getElementById('extend-path-btn');
        if (extendBtn) {
            extendBtn.onclick = async () => {
                // TODO: Implement path extension UI
                window.showToast('Path extension not yet implemented', 'info', 3000);
            };
        }
    }

    // === Extend Path Tool Methods ===

    /**
     * Enter extend-path tool mode - prepare for route extension
     */
    enterExtendRouteMode() {
        this.extendPathState = {
            active: false,  // Not active until a route is selected
            selectedPathId: null,
            selectedPath: null,
            reachableEndpoints: [],
            reachablePositions: new Set()
        };
        document.getElementById('status-state').textContent = 'Extend Path: Click on a path to extend';
    }

    /**
     * Exit extend-path tool mode - cleanup
     */
    exitExtendRouteMode() {
        this.resetExtendRouteState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    /**
     * Reset extend-route tool state
     */
    resetExtendRouteState() {
        this.extendPathState = {
            active: false,
            selectedPathId: null,
            selectedPath: null,
            reachableEndpoints: [],
            reachablePositions: new Set()
        };
        // Clear renderer highlights
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.renderer.clearPathHighlight) {
            this.renderer.clearPathHighlight();
        }
        // Hide secondary sidebar if showing extend route setup
        if (this._currentEditorType === 'extend-path-setup') {
            this.hideSecondarySidebar();
        }
        // Redraw
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }
    }

    /**
     * Handle click in extend-path tool mode
     */
    async handleExtendRouteToolClick(gridPos) {
        // Find the track at this position
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);

        if (!this.extendPathState?.active) {
            // First click - select a route by clicking on a tile that's part of it
            await this.handleExtendRouteFirstClick(gridPos, track);
        } else {
            // Second click - select new destination
            await this.handleExtendRouteSecondClick(gridPos, track);
        }
    }

    /**
     * Handle first click in extend-route tool - select a route to extend
     * The route can only be extended from its END block (following direction)
     */
    async handleExtendRouteFirstClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('No block at this position', 'warning', 3000);
            return;
        }

        // Find active routes where this block is the END block (can only extend from end)
        try {
            const result = await this.api.getPaths();
            const paths = result.paths || [];
            
            // Find paths where this block is the END block (direction constraint)
            const matchingPaths = paths.filter(p => 
                p.end_block_id === blockId && 
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                // Check if this block is part of a path but not the end
                const partOfPath = paths.find(p => 
                    p.block_sequence?.includes(blockId) && 
                    ['pending', 'setting', 'set', 'active'].includes(p.status)
                );
                
                if (partOfPath) {
                    window.showToast('Can only extend from the END of a path. Click on the end block.', 'warning', 4000);
                } else {
                    window.showToast('No active path ends at this block', 'warning', 3000);
                }
                return;
            }

            // For simplicity, select the first matching path
            const selectedPath = matchingPaths[0];
            
            this.extendPathState.selectedPathId = selectedPath.path_id;
            this.extendPathState.selectedPath = selectedPath;
            this.extendPathState.active = true;

            // Highlight the selected route
            if (this.renderer.highlightPath) {
                this.renderer.highlightPath(selectedPath);
            }

            // Fetch reachable endpoints in the FORWARD direction only using route-specific API
            document.getElementById('status-state').textContent = 'Extend Path: Loading destinations...';
            const endpointsResult = await this.api.getPathReachableExtensions(selectedPath.path_id);
            
            // Check for API error
            if (endpointsResult.error) {
                window.showToast('Cannot extend path: ' + endpointsResult.error, 'error', 5000);
                this.resetExtendRouteState();
                return;
            }
            
            this.extendPathState.reachableEndpoints = endpointsResult.reachable_endpoints || [];
            this.extendPathState.exitDirection = endpointsResult.exit_direction;

            // Build set of reachable tile positions for rendering
            this.buildExtendRoutePositionsSet();

            // Update status
            const count = this.extendPathState.reachableEndpoints.length;
            document.getElementById('status-state').textContent = `Extend Path: Click new destination (${count} reachable)`;

            // Set renderer highlights
            if (this.renderer.setReachableHighlights) {
                this.renderer.setReachableHighlights(this.extendPathState.reachablePositions);
            }

            // Show extend route panel
            this.showExtendRoutePanel();

            // Redraw to show highlights
            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            }
        } catch (err) {
            console.error('[Editor] Failed to get paths for extend:', err);
            window.showToast('Failed to load paths: ' + err.message, 'error', 5000);
            this.resetExtendRouteState();
        }
    }

    /**
     * Build set of tile positions for reachable blocks (for extend route)
     */
    buildExtendRoutePositionsSet() {
        const positions = new Set();
        const tracks = this.cachedNetwork?.tracks || [];

        for (const endpoint of this.extendPathState.reachableEndpoints) {
            for (const track of tracks) {
                if (track.block_section_id === endpoint) {
                    positions.add(`${track.x},${track.y}`);
                }
            }
        }

        this.extendPathState.reachablePositions = positions;
    }

    /**
     * Handle second click in extend-route tool - select new destination
     */
    async handleExtendRouteSecondClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const newEndBlockId = track.block_section_id;
        if (!newEndBlockId) {
            window.showToast('Must select a block as destination', 'warning', 3000);
            return;
        }

        // Check if endpoint is reachable
        if (!this.extendPathState.reachableEndpoints.includes(newEndBlockId)) {
            window.showToast('Cannot extend to this block', 'warning', 3000);
            return;
        }

        // Extend the route
        try {
            document.getElementById('status-state').textContent = 'Extend Path: Extending...';
            
            const result = await this.api.extendRoute(
                this.extendPathState.selectedPathId,
                newEndBlockId
            );

            if (result.success) {
                window.showToast(`Route extended to ${newEndBlockId}`, 'success', 5000);
                await window.app.refreshNetwork();
                this.resetExtendRouteState();
                document.getElementById('status-state').textContent = 'Extend Path: Click on a path to extend';
            } else {
                window.showToast(`Extend failed: ${result.error || result.message}`, 'error', 5000);
            }
        } catch (err) {
            console.error('[Editor] Failed to extend route:', err);
            window.showToast('Failed to extend route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Show extend route panel in secondary sidebar
     */
    showExtendRoutePanel() {
        this._currentEditorType = 'extend-path-setup';
        this._currentEditorId = this.extendPathState.selectedPathId;

        const route = this.extendPathState.selectedPath;
        const statusColor = this.getPathStatusColor(path.status);
        const exitDir = this.extendPathState.exitDirection;
        const exitDirText = exitDir ? this.getDirectionName(exitDir) : 'Unknown';
        const reachableCount = this.extendPathState.reachableEndpoints.length;

        const content = `
            <div class="extend-route-panel">
                ${renderHeaderId(path.path_id, statusColor)}
                
                ${renderReadOnlyValue('Current End', path.end_block_id, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('Direction', `${exitDirText} (${exitDir || '?'})`, 'text-info font-bold')}
                ${renderReadOnlyValue('Status', path.status.toUpperCase(), `font-bold`)}
                ${path.train_id
                    ? renderReadOnlyValue('Train', `${path.train_id}`, 'text-info font-bold')
                    : ''
                }
                
                <div class="info-box text-sm" style="margin-top: 16px;">
                    ${reachableCount > 0 
                        ? `Click on an <span style="color: #ff9500;">orange</span> destination block to extend the route ${exitDirText.toLowerCase()}.`
                        : `<span style="color: #ff6b6b;">No forward destinations available.</span> The route may be at a dead end or all forward blocks are occupied.`
                    }
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

        this.showSecondarySidebar('+ Extend Route', content);
        
        // Add compact class
        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.add('compact');
        }
    }

    /**
     * Get human-readable direction name
     */
    getDirectionName(dir) {
        const names = { 'N': 'North', 'S': 'South', 'E': 'East', 'W': 'West' };
        return names[dir] || dir || 'Unknown';
    }

    // === Cancel Route Tool Methods ===

    /**
     * Enter cancel-path tool mode
     */
    enterCancelRouteMode() {
        document.getElementById('status-state').textContent = 'Cancel Route: Click on a route to cancel';
    }

    /**
     * Exit cancel-path tool mode
     */
    exitCancelRouteMode() {
        document.getElementById('status-state').textContent = 'Ready';
    }

    /**
     * Handle click in cancel-path tool mode
     */
    async handleCancelRouteToolClick(gridPos) {
        // Find the track at this position
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);
        
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('No block at this position', 'warning', 3000);
            return;
        }

        // Find active paths that contain this block
        try {
            const result = await this.api.getPaths();
            const paths = result.paths || [];
            
            // Find paths where this block is in the block_sequence
            const matchingPaths = paths.filter(p => 
                p.block_sequence?.includes(blockId) && 
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                window.showToast('No active path found at this block', 'warning', 3000);
                return;
            }

            // For simplicity, select the first matching path
            const pathToCancel = matchingPaths[0];
            
            // Show confirmation
            const msg = `Cancel path "${pathToCancel.path_id}"?\n\n` +
                `From: ${pathToCancel.start_block_id}\n` +
                `To: ${pathToCancel.end_block_id}` +
                (pathToCancel.train_id ? `\nTrain: ${pathToCancel.train_id}` : '');
            
            if (!confirm(msg)) {
                return;
            }

            // Cancel the path
            document.getElementById('status-state').textContent = 'Cancel Path: Cancelling...';
            
            const cancelResult = await this.api.cancelPath(pathToCancel.path_id);
            
            if (cancelResult.success) {
                window.showToast(`Path ${pathToCancel.path_id} cancelled`, 'success', 5000);
                await window.app.refreshNetwork();
            } else {
                window.showToast(`Failed to cancel: ${cancelResult.message || cancelResult.error}`, 'error', 5000);
            }
            
            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        } catch (err) {
            console.error('[Editor] Failed to cancel path:', err);
            window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        }
    }
}
