// ============================================================================
// pathfinders.js - Pathfinding and Track Construction Utilities
// ============================================================================
// Requires: constants.js (must be loaded first)
// ============================================================================

function pathsToTrackBits(directedPaths) {
    const pathMap = {
        'NS': TRACK_NS, 'SN': TRACK_NS,
        'EW': TRACK_EW, 'WE': TRACK_EW,
        'NE': TRACK_NE, 'EN': TRACK_NE,
        'NW': TRACK_NW, 'WN': TRACK_NW,
        'SE': TRACK_SE, 'ES': TRACK_SE,
        'SW': TRACK_SW, 'WS': TRACK_SW
    };

    const toShort = (port) => port ? port.charAt(0) : '';

    let bits = 0;
    for (const path of directedPaths) {
        const key = toShort(path.port1) + toShort(path.port2);
        bits |= pathMap[key] || 0;
    }
    return bits;
}

class ConstructionPathfinder {
    constructor() {
        this.costs = {
            useExisting: 0.2,
            extend: 0.5,
            diagonal: 1,
            straight: 2,
            sameTurn: 3,
            createSwitch: 5,
            createCrossover: 7,
            reverse: 100
        };

        // Avoid-connect mode (shift key): flip the incentives so the path
        // routes through existing tiles as a crossover instead of merging
        // into a switch. createSwitch becomes prohibitive, reusing an
        // existing edge is heavily penalised, and createCrossover gets a
        // negative cost so A* actively prefers cutting through over
        // sidestepping. The -2 value is tuned so A* crosses when the path
        // naturally would pass through existing track, but does not detour
        // to chase a crossing when none is needed.
        this.avoidConnectCosts = {
            useExisting: 50,
            extend: 50,
            diagonal: 1,
            straight: 2,
            sameTurn: 3,
            createSwitch: 200,
            createCrossover: -2,
            reverse: 100
        };

        this.TRACK_NS = 1;
        this.TRACK_EW = 2;
        this.TRACK_NE = 4;
        this.TRACK_NW = 8;
        this.TRACK_SE = 16;
        this.TRACK_SW = 32;
    }

    getTrackBitForPath(fromDir, toDir) {
        // fromDir = movement direction by which we arrived at the tile;
        //   the entry edge is the opposite of that direction.
        // toDir = movement direction by which we leave the tile;
        //   the exit edge equals that direction.
        const entryEdge = this.getOppositeDir(fromDir);
        const exitEdge = toDir;

        const key = [entryEdge, exitEdge].sort().join('');
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

    detectSwitchType(existingBits, newBit) {
        if (!existingBits || !newBit) return 'none';
        if (existingBits & newBit) return 'none';

        const combined = existingBits | newBit;
        const existingCount = this.countTrackSegments(existingBits);
        const newCount = this.countTrackSegments(combined);

        if (newCount <= existingCount) return 'none';

        const crossovers = [
            [this.TRACK_NS, this.TRACK_EW],
            [this.TRACK_NE, this.TRACK_SW],
            [this.TRACK_NW, this.TRACK_SE]
        ];

        for (const [a, b] of crossovers) {
            if ((combined & a) && (combined & b)) {
                if ((existingBits & a) && (newBit & b)) return 'crossover';
                if ((existingBits & b) && (newBit & a)) return 'crossover';
            }
        }

        if (existingCount >= 1 && newCount > existingCount) {
            return 'switch';
        }

        return 'none';
    }

    getExistingTrackDirections(trackBits) {
        const directions = [];
        if (trackBits & 1) { directions.push('NORTH', 'SOUTH'); }
        if (trackBits & 2) { directions.push('EAST', 'WEST'); }
        if (trackBits & 4) { directions.push('NORTH', 'EAST'); }
        if (trackBits & 8) { directions.push('NORTH', 'WEST'); }
        if (trackBits & 16) { directions.push('SOUTH', 'EAST'); }
        if (trackBits & 32) { directions.push('SOUTH', 'WEST'); }
        return [...new Set(directions)];
    }

    // Given an entry edge into a tile, return the valid exit edges
    // respecting segment binding. Each track_bits segment is one of 6 fixed
    // edge pairs; entering through one edge of a segment yields the OTHER
    // edge of THAT segment as the exit. For a switch tile (multiple
    // segments set) the entry edge may belong to more than one segment,
    // and each contributing segment yields its paired edge.
    //
    // Use this — not getExistingTrackDirections — when routing through
    // switches. getExistingTrackDirections flattens all segments into one
    // set, which lets a path "cheat" between disconnected segments of a
    // switch (e.g. enter SOUTH on NS-straight, exit EAST on NE-curve).
    getExitsForEntry(trackBits, entry) {
        const SEGMENTS = [
            [1,  'NORTH', 'SOUTH'],
            [2,  'EAST',  'WEST'],
            [4,  'NORTH', 'EAST'],
            [8,  'NORTH', 'WEST'],
            [16, 'SOUTH', 'EAST'],
            [32, 'SOUTH', 'WEST'],
        ];
        const exits = new Set();
        for (const [bit, a, b] of SEGMENTS) {
            if ((trackBits & bit) === 0) continue;
            if (a === entry) exits.add(b);
            else if (b === entry) exits.add(a);
        }
        return [...exits];
    }

    findPath(startX, startY, endX, endY, existingTracks, options = {}) {
        const costs = options.avoidConnect ? this.avoidConnectCosts : this.costs;
        const start = { x: startX, y: startY };
        const end = { x: endX, y: endY };

        // Forced-segment constraints: if the user pressed R/T to lock the
        // start/end tile to a specific 2-edge segment, A* must pick a path
        // whose first move leaves through one of the start segment's edges
        // and whose last move enters through one of the end segment's
        // edges. Otherwise the start/end tile would be laid disconnected
        // from the rest of the path.
        const SEGMENT_EDGES = {
            NS: ['NORTH', 'SOUTH'],
            EW: ['EAST', 'WEST'],
            NE: ['NORTH', 'EAST'],
            NW: ['NORTH', 'WEST'],
            SE: ['SOUTH', 'EAST'],
            SW: ['SOUTH', 'WEST'],
        };
        const startEdges = SEGMENT_EDGES[options.forcedStartSegment] || null;
        const endEdges   = SEGMENT_EDGES[options.forcedEndSegment]   || null;

        if (startX === endX && startY === endY) {
            return [start];
        }

        const trackMap = new Map();

        if (existingTracks) {
            existingTracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));
        }

        const endTrack = trackMap.get(`${endX},${endY}`);
        if (endTrack) {
            if (endTrack.type === 'station' || endTrack.station_id) {
                const stationId = endTrack.station_id;
                if (stationId && existingTracks) {
                    const stationTiles = existingTracks.filter(t => t.station_id === stationId);
                    if (stationTiles.length > 1) {
                        const isEndpoint = stationTiles.length <= 2 || this.isStationEndpoint(endX, endY, stationTiles);
                        if (!isEndpoint) {
                            return null;
                        }
                    }
                }
            }
        }

        const startTrack = trackMap.get(`${startX},${startY}`);
        const preferredDirs = startTrack ? this.getExistingTrackDirections(startTrack.track_bits || 0) : [];

        const openSet = [{
            pos: start,
            g: 0,
            h: this.heuristic(start, end),
            f: this.heuristic(start, end),
            parent: null,
            fromDir: null,
            prevTurnDir: null
        }];
        const closedSet = new Set();

        while (openSet.length > 0) {
            openSet.sort((a, b) => a.f - b.f);
            const current = openSet.shift();
            const key = `${current.pos.x},${current.pos.y}`;
            
            if (current.pos.x === end.x && current.pos.y === end.y) {
                const path = this.reconstructPath(current);
                // alignZigzagTails inserts a tile adjacent to the end to make
                // zigzag endings look smooth; that insertion ignores R/T
                // constraints and can also break adjacency, so skip it when
                // either endpoint is locked.
                if (startEdges || endEdges) return path;
                return this.alignZigzagTails(path, end);
            }
            
            closedSet.add(key);

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
                const entryDir = this.getOppositeDir(neighbor.dir);

                // Forced start segment: the first move out of the start
                // tile must exit through one of the forced segment's edges.
                if (startEdges && current.fromDir === null && !startEdges.includes(neighbor.dir)) {
                    continue;
                }

                // Forced end segment: the move that lands on the end tile
                // must enter through one of the forced end segment's edges
                // (i.e., the entry edge of the end tile must be in endEdges).
                if (endEdges && neighbor.x === end.x && neighbor.y === end.y && !endEdges.includes(entryDir)) {
                    continue;
                }
                
                if (existingTrack && (existingTrack.type === 'signal' || existingTrack.signal_state)) {
                    const signalPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
                    if (!signalPorts.includes(entryDir)) {
                        continue;
                    }
                }
                
                if (existingTrack && (existingTrack.type === 'station' || existingTrack.station_id)) {
                    const stationPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
                    if (!stationPorts.includes(entryDir)) {
                        continue;
                    }
                }

                let moveCost = costs.straight;
                let turnDir = null;

                if (existingTrack && existingTrack.track_bits) {
                    const trackDirs = this.getExistingTrackDirections(existingTrack.track_bits);
                    const entryDir = this.getOppositeDir(neighbor.dir);
                    if (trackDirs.includes(entryDir)) {
                        moveCost = costs.useExisting;
                    }
                } else if (!current.fromDir) {
                    if (preferredDirs.length > 0 && preferredDirs.includes(neighbor.dir)) {
                        moveCost = costs.extend;
                    }
                } else if (this.isReverse(current.fromDir, neighbor.dir)) {
                    moveCost = costs.reverse;
                } else if (current.fromDir !== neighbor.dir) {
                    turnDir = this.getTurnDirection(current.fromDir, neighbor.dir);
                    if (current.prevTurnDir && turnDir === current.prevTurnDir) {
                        moveCost = costs.sameTurn;
                    } else {
                        moveCost = costs.diagonal;
                    }
                }

                if (current.fromDir) {
                    const currentTileTrack = trackMap.get(key);
                    const currentBits = currentTileTrack ? currentTileTrack.track_bits : 0;
                    const newPathBit = this.getTrackBitForPath(current.fromDir, neighbor.dir);
                    const switchType = this.detectSwitchType(currentBits, newPathBit);
                    if (switchType === 'crossover') {
                        moveCost += costs.createCrossover;
                    } else if (switchType === 'switch') {
                        moveCost += costs.createSwitch;
                    }
                }

                const g = current.g + moveCost;
                const h = this.heuristic(neighbor, end);
                const f = g + h;

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

        return this.diagonalLine(start, end);
    }

    heuristic(a, b) {
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
        
        return adjacentStationCount <= 1;
    }

    getTurnDirection(fromDir, toDir) {
        const turns = {
            'NORTH': { 'EAST': 'RIGHT', 'WEST': 'LEFT' },
            'EAST': { 'SOUTH': 'RIGHT', 'NORTH': 'LEFT' },
            'SOUTH': { 'WEST': 'RIGHT', 'EAST': 'LEFT' },
            'WEST': { 'NORTH': 'RIGHT', 'SOUTH': 'LEFT' }
        };
        if (!turns[fromDir]) {
            return null;
        }
        return turns[fromDir][toDir] || null;
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

    detectZigzagPattern(path) {
        if (path.length < 4) return null;

        const directions = [];
        for (let i = 1; i < path.length; i++) {
            const dx = path[i].x - path[i-1].x;
            const dy = path[i].y - path[i-1].y;
            if (dy > 0) directions.push('NORTH');
            else if (dy < 0) directions.push('SOUTH');
            else if (dx > 0) directions.push('EAST');
            else if (dx < 0) directions.push('WEST');
        }

        const start = Math.floor(directions.length * 0.2);
        const end = Math.floor(directions.length * 0.8);
        const middle = directions.slice(start, end);

        if (middle.length < 2) return null;

        const uniqueDirs = [...new Set(middle)];
        if (uniqueDirs.length !== 2) return null;

        let alternates = true;
        for (let i = 1; i < middle.length && alternates; i++) {
            if (middle[i] === middle[i-1]) alternates = false;
        }

        return alternates ? uniqueDirs : null;
    }

    alignZigzagTails(path, end) {
        if (path.length < 3) return path;

        const pattern = this.detectZigzagPattern(path);
        if (!pattern) return path;

        let result = [...path];

        if (result.length >= 2) {
            const firstMove = {
                dx: result[1].x - result[0].x,
                dy: result[1].y - result[0].y
            };
            const firstDir = firstMove.dy > 0 ? 'NORTH' : (firstMove.dy < 0 ? 'SOUTH' : (firstMove.dx > 0 ? 'EAST' : 'WEST'));

            if (!pattern.includes(firstDir)) {
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

                if (insertPos.x !== result[1].x || insertPos.y !== result[1].y) {
                    result.splice(1, 0, insertPos);
                }
            }
        }

        if (result.length >= 2) {
            const lastIdx = result.length - 1;
            const lastMove = {
                dx: result[lastIdx].x - result[lastIdx - 1].x,
                dy: result[lastIdx].y - result[lastIdx - 1].y
            };
            const lastDir = lastMove.dy > 0 ? 'NORTH' : (lastMove.dy < 0 ? 'SOUTH' : (lastMove.dx > 0 ? 'EAST' : 'WEST'));

            if (!pattern.includes(lastDir) && result.length >= 3) {
                const prevPos = result[lastIdx - 1];
                const p0 = pattern[0];
                const delta0 = this.getDelta(p0);
                const penultimate0 = { x: end.x - delta0.dx, y: end.y - delta0.dy };

                const p1 = pattern[1];
                const delta1 = this.getDelta(p1);
                const penultimate1 = { x: end.x - delta1.dx, y: end.y - delta1.dy };

                const dist0 = Math.abs(penultimate0.x - prevPos.x) + Math.abs(penultimate0.y - prevPos.y);
                const dist1 = Math.abs(penultimate1.x - prevPos.x) + Math.abs(penultimate1.y - prevPos.y);

                const insertPos = dist0 <= dist1 ? penultimate0 : penultimate1;

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

// Forced-segment shape -> (port1, port2). When R/T are used during a drag,
// the editor passes the chosen name in options.forcedStartSegment /
// forcedEndSegment and we lay exactly that segment on the start/end tile.
const FORCED_SEGMENT_PORTS = {
    NS: ['NORTH', 'SOUTH'],
    EW: ['EAST', 'WEST'],
    NE: ['NORTH', 'EAST'],
    NW: ['NORTH', 'WEST'],
    SE: ['SOUTH', 'EAST'],
    SW: ['SOUTH', 'WEST'],
};

function pathToTracks(path, cachedNetwork, options = {}) {
    if (!path || path.length === 0) return [];

    const avoidConnect = options.avoidConnect === true;
    const forcedStart = options.forcedStartSegment || null;
    const forcedEnd = options.forcedEndSegment || null;

    const tracksToUpdate = new Map();
    const existingTracks = new Map();
    
    if (cachedNetwork && cachedNetwork.tracks) {
        for (const track of cachedNetwork.tracks) {
            existingTracks.set(`${track.x},${track.y}`, track);
        }
    }
    
    function neighborHasPortTo(neighborX, neighborY, portName) {
        const neighborTrack = existingTracks.get(`${neighborX},${neighborY}`);
        if (!neighborTrack) return false;
        
        if (neighborTrack.directed_paths) {
            return neighborTrack.directed_paths.some(p => 
                p.port1 === portName || p.port2 === portName
            );
        }
        if (neighborTrack.paths) {
            return neighborTrack.paths.some(p => p.includes(portName));
        }
        if (neighborTrack.connections) {
            return portName in neighborTrack.connections;
        }
        return false;
    }
    
    function addPath(x, y, port1, port2, direction = 'both') {
        const key = `${x},${y}`;
        if (!tracksToUpdate.has(key)) {
                const existing = existingTracks.get(key);
                const existingType = existing ? existing.type : null;
                tracksToUpdate.set(key, {
                    x, y,
                    type: existingType || 'STRAIGHT',
                    directed_paths: []
                });
        }
        const track = tracksToUpdate.get(key);
        
        const pathExists = track.directed_paths.some(p => 
            (p.port1 === port1 && p.port2 === port2) ||
            (p.port1 === port2 && p.port2 === port1)
        );
        if (!pathExists && port1 !== port2) {
            track.directed_paths.push({ port1, port2, direction });
        }
    }
    
    for (let i = 0; i < path.length; i++) {
        const current = path[i];

        const existingForProtection = existingTracks.get(`${current.x},${current.y}`);
        if (existingForProtection && (existingForProtection.type === 'station' || existingForProtection.station_id)) {
            continue;
        }
        if (existingForProtection && (existingForProtection.type === 'signal' || existingForProtection.signal_state)) {
            continue;
        }

        const prev = i > 0 ? path[i - 1] : null;
        const next = i < path.length - 1 ? path[i + 1] : null;

        const entryPort = prev ? getDirection(current, prev) : null;
        const exitPort = next ? getDirection(current, next) : null;

        if (prev && next) {
            addPath(current.x, current.y, entryPort, exitPort, 'both');
            continue;
        }

        if (!prev && next) {
            if (forcedStart && FORCED_SEGMENT_PORTS[forcedStart]) {
                const [p1, p2] = FORCED_SEGMENT_PORTS[forcedStart];
                addPath(current.x, current.y, p1, p2, 'both');
                continue;
            }

            const exitDir = getDirection(current, next);
            const entryDir = getOpposite(exitDir);

            let foundConnection = false;

            const existingTrack = existingTracks.get(`${current.x},${current.y}`);
            if (existingTrack && existingTrack.track_bits) {
                const getConnectedPort = (bits, port) => {
                    if ((bits & 1) && (port === 'NORTH' || port === 'SOUTH')) return port === 'NORTH' ? 'SOUTH' : 'NORTH';
                    if ((bits & 2) && (port === 'EAST' || port === 'WEST')) return port === 'EAST' ? 'WEST' : 'EAST';
                    if ((bits & 4) && (port === 'NORTH' || port === 'EAST')) return port === 'NORTH' ? 'EAST' : 'NORTH';
                    if ((bits & 8) && (port === 'NORTH' || port === 'WEST')) return port === 'NORTH' ? 'WEST' : 'NORTH';
                    if ((bits & 16) && (port === 'SOUTH' || port === 'EAST')) return port === 'SOUTH' ? 'EAST' : 'SOUTH';
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
                const neighbors = [
                    { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
                    { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
                    { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
                    { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' }
                ];

                for (const n of neighbors) {
                    if (n.myPort === exitDir) continue;

                    if (neighborHasPortTo(current.x + n.dx, current.y + n.dy, n.theirPort)) {
                        addPath(current.x, current.y, n.myPort, exitDir, 'both');
                        foundConnection = true;
                    }
                }
            }

            if (!foundConnection) {
                addPath(current.x, current.y, entryDir, exitDir, 'both');
            }
            continue;
        }
        
        if (prev && !next) {
            if (forcedEnd && FORCED_SEGMENT_PORTS[forcedEnd]) {
                const [p1, p2] = FORCED_SEGMENT_PORTS[forcedEnd];
                addPath(current.x, current.y, p1, p2, 'both');
                continue;
            }

            const entryDir = getDirection(current, prev);
            const exitDir = getOpposite(entryDir);

            if (avoidConnect) {
                // Don't extend toward neighbors; pick an exit edge that does
                // not share with any existing segment on this tile so the
                // result is a crossover (or a clean stub) instead of a switch.
                const endTrack = existingTracks.get(`${current.x},${current.y}`);
                const endBits = endTrack ? (endTrack.track_bits || 0) : 0;
                const avoidExit = computeNonConflictingExit(endBits, entryDir);
                addPath(current.x, current.y, entryDir, avoidExit, 'both');
                continue;
            }

            const neighbors = [
                { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
                { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
                { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
                { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' }
            ];

            let foundConnection = false;
            for (const n of neighbors) {
                if (n.myPort === entryDir) continue;

                if (neighborHasPortTo(current.x + n.dx, current.y + n.dy, n.theirPort)) {
                    addPath(current.x, current.y, entryDir, n.myPort, 'both');
                    foundConnection = true;
                }
            }

            if (!foundConnection) {
                addPath(current.x, current.y, entryDir, exitDir, 'both');
            }
            continue;
        }
        
        if (!prev && !next) {
            // Single-tile drag (mousedown == mouseup): honor either or both
            // forced segments so the user can place a crossover on one tile.
            // Falls back to the default EW stub when neither is set.
            if (forcedStart && FORCED_SEGMENT_PORTS[forcedStart]) {
                const [p1, p2] = FORCED_SEGMENT_PORTS[forcedStart];
                addPath(current.x, current.y, p1, p2, 'both');
            }
            if (forcedEnd && forcedEnd !== forcedStart && FORCED_SEGMENT_PORTS[forcedEnd]) {
                const [p1, p2] = FORCED_SEGMENT_PORTS[forcedEnd];
                addPath(current.x, current.y, p1, p2, 'both');
            }
            if (!forcedStart && !forcedEnd) {
                addPath(current.x, current.y, 'WEST', 'EAST', 'both');
            }
        }
    }
    
    const tracks = [];
    for (const track of tracksToUpdate.values()) {
        const trackBits = pathsToTrackBits(track.directed_paths);

        if (FORBIDDEN_TRACK_BITS.has(trackBits)) {
            console.warn(`[pathToTracks] Skipping forbidden configuration (${trackBits}) at (${track.x}, ${track.y})`);
            if (window.showError) {
                window.showToast(`Cannot create forbidden configuration (${trackBits}). This would require a 3-way switch.`, 'error', 5000);
            }
            continue;
        }

        track.track_bits = trackBits;
        tracks.push(track);
    }

    return tracks;
}

// For avoid-connect mode: given existing track bits on a tile and the edge
// the path arrives on, return an exit edge that does not share an edge with
// any existing segment when possible. Falls back to the opposite edge so the
// caller always gets a usable port.
function computeNonConflictingExit(existingBits, entryDir) {
    const opposite = getOpposite(entryDir);
    if (!existingBits) return opposite;

    const used = new Set();
    if (existingBits & 1)  { used.add('NORTH'); used.add('SOUTH'); }
    if (existingBits & 2)  { used.add('EAST');  used.add('WEST'); }
    if (existingBits & 4)  { used.add('NORTH'); used.add('EAST'); }
    if (existingBits & 8)  { used.add('NORTH'); used.add('WEST'); }
    if (existingBits & 16) { used.add('SOUTH'); used.add('EAST'); }
    if (existingBits & 32) { used.add('SOUTH'); used.add('WEST'); }

    if (used.has(entryDir)) return opposite;

    for (const dir of ['NORTH', 'EAST', 'SOUTH', 'WEST']) {
        if (dir !== entryDir && !used.has(dir)) return dir;
    }
    return opposite;
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
// Signal Placement Validation
// ============================================================================

const VALID_DETECTION_DIRECTIONS = {
    1:  ['NORTH', 'SOUTH'],
    2:  ['EAST', 'WEST'],
    4:  ['NORTH', 'EAST'],
    8:  ['NORTH', 'WEST'],
    16: ['SOUTH', 'EAST'],
    32: ['SOUTH', 'WEST'],
};

const SWITCH_TRACK_BITS = new Set([
    3, 24, 36,
    5, 9, 17, 33,
    6, 10, 18, 34,
    12, 20, 40, 48,
    7, 11, 19, 35, 14, 38, 26, 50, 21, 37, 25, 41
]);

function getValidSignalDirections(trackBits) {
    if (SWITCH_TRACK_BITS.has(trackBits)) {
        return [];
    }
    return VALID_DETECTION_DIRECTIONS[trackBits] || [];
}

// ============================================================================
// Block Assignment Validation
// ============================================================================

function isCrossingForBlock(trackBits) {
    return trackBits === 3 || trackBits === 24 || trackBits === 36;
}

function validateBlockTile(track) {
    if (!track) {
        return { valid: false, reason: 'No track at this position' };
    }

    if (track.is_switch) {
        return { valid: false, reason: 'Switches cannot be inside a block' };
    }

    if (isCrossingForBlock(track.track_bits)) {
        return { valid: false, reason: 'Crossings cannot be inside a block - paths are isolated' };
    }

    return { valid: true, reason: null };
}

function validateBlockPath(path, tracks, mode = 'create') {
    const warnings = [];
    const errors = [];
    const blocksInPath = new Set();
    const signalsInPath = [];
    const trackMap = new Map();

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

        if (track && track.block_section_id) {
            blocksInPath.add(track.block_section_id);
        }

        if (track && (track.signal_state || track.type === 'signal')) {
            hasSignal = true;
            signalsInPath.push({ x: pos.x, y: pos.y, track });
        }
    }

    if (mode === 'create') {
        if (blocksInPath.size > 0) {
            errors.push(`Path contains tracks already assigned to block(s): ${[...blocksInPath].join(', ')}`);
        }
    } else if (mode === 'extend') {
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

