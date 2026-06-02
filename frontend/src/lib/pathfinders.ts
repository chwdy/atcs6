// pathfinders.ts — Pathfinding and track-construction utilities (editor preview).
//
// Migrated from static/js/pathfinders.js (longterm_todo.md item #7, Wave 1 —
// foundation cluster). Behavior is unchanged from the classic file.
//
// `ConstructionPathfinder.getExitsForEntry` is pinned against the Python
// backend by tests/test_pathfinder_frontend_parity.py — its SEGMENTS table must
// stay aligned with the backend's segment-binding rule.
//
// Part of the Vite bundle; the editor handlers import these utilities directly.

import {
  TRACK_NS,
  TRACK_EW,
  TRACK_NE,
  TRACK_NW,
  TRACK_SE,
  TRACK_SW,
  FORBIDDEN_TRACK_BITS,
} from './constants';

// ---- Light shapes the pathfinder reads off otherwise-dynamic objects --------

export interface Point {
  x: number;
  y: number;
}

interface DirectedPath {
  port1: string | null;
  port2: string | null;
  direction?: string;
}

interface TrackData {
  x: number;
  y: number;
  track_bits?: number;
  type?: string;
  station_id?: string;
  signal_state?: unknown;
  block_section_id?: string;
  is_switch?: boolean;
  directed_paths?: DirectedPath[];
  paths?: string[][];
  connections?: Record<string, unknown>;
}

interface CostTable {
  useExisting: number;
  extend: number;
  diagonal: number;
  straight: number;
  sameTurn: number;
  createSwitch: number;
  createCrossover: number;
  reverse: number;
}

interface AStarNode {
  pos: Point;
  g: number;
  h: number;
  f: number;
  parent: AStarNode | null;
  fromDir: string | null;
  prevTurnDir: string | null;
}

interface FindPathOptions {
  avoidConnect?: boolean;
  forcedStartSegment?: string;
  forcedEndSegment?: string;
}

interface PathToTracksOptions {
  avoidConnect?: boolean;
  forcedStartSegment?: string | null;
  forcedEndSegment?: string | null;
}

export function pathsToTrackBits(directedPaths: DirectedPath[]): number {
  const pathMap: Record<string, number> = {
    NS: TRACK_NS,
    SN: TRACK_NS,
    EW: TRACK_EW,
    WE: TRACK_EW,
    NE: TRACK_NE,
    EN: TRACK_NE,
    NW: TRACK_NW,
    WN: TRACK_NW,
    SE: TRACK_SE,
    ES: TRACK_SE,
    SW: TRACK_SW,
    WS: TRACK_SW,
  };

  const toShort = (port: string | null) => (port ? port.charAt(0) : '');

  let bits = 0;
  for (const path of directedPaths) {
    const key = toShort(path.port1) + toShort(path.port2);
    bits |= pathMap[key] || 0;
  }
  return bits;
}

export class ConstructionPathfinder {
  costs: CostTable;
  avoidConnectCosts: CostTable;
  TRACK_NS: number;
  TRACK_EW: number;
  TRACK_NE: number;
  TRACK_NW: number;
  TRACK_SE: number;
  TRACK_SW: number;

  constructor() {
    this.costs = {
      useExisting: 0.2,
      extend: 0.5,
      diagonal: 1,
      straight: 2,
      sameTurn: 3,
      createSwitch: 5,
      createCrossover: 7,
      reverse: 100,
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
      reverse: 100,
    };

    this.TRACK_NS = 1;
    this.TRACK_EW = 2;
    this.TRACK_NE = 4;
    this.TRACK_NW = 8;
    this.TRACK_SE = 16;
    this.TRACK_SW = 32;
  }

  getTrackBitForPath(fromDir: string, toDir: string): number {
    // fromDir = movement direction by which we arrived at the tile;
    //   the entry edge is the opposite of that direction.
    // toDir = movement direction by which we leave the tile;
    //   the exit edge equals that direction.
    const entryEdge = this.getOppositeDir(fromDir);
    const exitEdge = toDir;

    const key = [entryEdge, exitEdge].sort().join('');
    const pathMap: Record<string, number> = {
      NORTHSOUTH: this.TRACK_NS,
      EASTWEST: this.TRACK_EW,
      EASTNORTH: this.TRACK_NE,
      NORTHWEST: this.TRACK_NW,
      EASTSOUTH: this.TRACK_SE,
      SOUTHWEST: this.TRACK_SW,
    };
    return pathMap[key] || 0;
  }

  countTrackSegments(trackBits: number): number {
    let count = 0;
    if (trackBits & this.TRACK_NS) count++;
    if (trackBits & this.TRACK_EW) count++;
    if (trackBits & this.TRACK_NE) count++;
    if (trackBits & this.TRACK_NW) count++;
    if (trackBits & this.TRACK_SE) count++;
    if (trackBits & this.TRACK_SW) count++;
    return count;
  }

  detectSwitchType(existingBits: number, newBit: number): string {
    if (!existingBits || !newBit) return 'none';
    if (existingBits & newBit) return 'none';

    const combined = existingBits | newBit;
    const existingCount = this.countTrackSegments(existingBits);
    const newCount = this.countTrackSegments(combined);

    if (newCount <= existingCount) return 'none';

    const crossovers = [
      [this.TRACK_NS, this.TRACK_EW],
      [this.TRACK_NE, this.TRACK_SW],
      [this.TRACK_NW, this.TRACK_SE],
    ];

    for (const [a, b] of crossovers) {
      if (combined & a && combined & b) {
        if (existingBits & a && newBit & b) return 'crossover';
        if (existingBits & b && newBit & a) return 'crossover';
      }
    }

    if (existingCount >= 1 && newCount > existingCount) {
      return 'switch';
    }

    return 'none';
  }

  getExistingTrackDirections(trackBits: number): string[] {
    const directions: string[] = [];
    if (trackBits & 1) {
      directions.push('NORTH', 'SOUTH');
    }
    if (trackBits & 2) {
      directions.push('EAST', 'WEST');
    }
    if (trackBits & 4) {
      directions.push('NORTH', 'EAST');
    }
    if (trackBits & 8) {
      directions.push('NORTH', 'WEST');
    }
    if (trackBits & 16) {
      directions.push('SOUTH', 'EAST');
    }
    if (trackBits & 32) {
      directions.push('SOUTH', 'WEST');
    }
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
  getExitsForEntry(trackBits: number, entry: string): string[] {
    const SEGMENTS: Array<[number, string, string]> = [
      [1, 'NORTH', 'SOUTH'],
      [2, 'EAST', 'WEST'],
      [4, 'NORTH', 'EAST'],
      [8, 'NORTH', 'WEST'],
      [16, 'SOUTH', 'EAST'],
      [32, 'SOUTH', 'WEST'],
    ];
    const exits = new Set<string>();
    for (const [bit, a, b] of SEGMENTS) {
      if ((trackBits & bit) === 0) continue;
      if (a === entry) exits.add(b);
      else if (b === entry) exits.add(a);
    }
    return [...exits];
  }

  findPath(
    startX: number,
    startY: number,
    endX: number,
    endY: number,
    existingTracks: TrackData[] | null | undefined,
    options: FindPathOptions = {}
  ): Point[] | null {
    const costs = options.avoidConnect ? this.avoidConnectCosts : this.costs;
    const start = { x: startX, y: startY };
    const end = { x: endX, y: endY };

    // Forced-segment constraints: if the user pressed R/T to lock the
    // start/end tile to a specific 2-edge segment, A* must pick a path
    // whose first move leaves through one of the start segment's edges
    // and whose last move enters through one of the end segment's
    // edges. Otherwise the start/end tile would be laid disconnected
    // from the rest of the path.
    const SEGMENT_EDGES: Record<string, string[]> = {
      NS: ['NORTH', 'SOUTH'],
      EW: ['EAST', 'WEST'],
      NE: ['NORTH', 'EAST'],
      NW: ['NORTH', 'WEST'],
      SE: ['SOUTH', 'EAST'],
      SW: ['SOUTH', 'WEST'],
    };
    const startEdges =
      (options.forcedStartSegment && SEGMENT_EDGES[options.forcedStartSegment]) || null;
    const endEdges = (options.forcedEndSegment && SEGMENT_EDGES[options.forcedEndSegment]) || null;

    if (startX === endX && startY === endY) {
      return [start];
    }

    // Bound the search to the start–end bounding box plus a margin. The grid is
    // unbounded, so a target the square-corner / no-self-intersection rules make
    // unreachable would otherwise fan A* across the plane forever (open set
    // grows without limit). The margin dwarfs any 45° easing detour a
    // point-to-point draw needs; beyond it there is no valid build to find.
    const SEARCH_MARGIN = 24;
    const boundMinX = Math.min(startX, endX) - SEARCH_MARGIN;
    const boundMaxX = Math.max(startX, endX) + SEARCH_MARGIN;
    const boundMinY = Math.min(startY, endY) - SEARCH_MARGIN;
    const boundMaxY = Math.max(startY, endY) + SEARCH_MARGIN;

    const trackMap = new Map<string, TrackData>();

    if (existingTracks) {
      existingTracks.forEach((t) => trackMap.set(`${t.x},${t.y}`, t));
    }

    const endTrack = trackMap.get(`${endX},${endY}`);
    if (endTrack) {
      if (endTrack.type === 'station' || endTrack.station_id) {
        const stationId = endTrack.station_id;
        if (stationId && existingTracks) {
          const stationTiles = existingTracks.filter((t) => t.station_id === stationId);
          if (stationTiles.length > 1) {
            const isEndpoint =
              stationTiles.length <= 2 || this.isStationEndpoint(endX, endY, stationTiles);
            if (!isEndpoint) {
              return null;
            }
          }
        }
      }
    }

    const startTrack = trackMap.get(`${startX},${startY}`);
    const preferredDirs = startTrack
      ? this.getExistingTrackDirections(startTrack.track_bits || 0)
      : [];

    // A* state is (tile, incoming direction): the same tile reached from a
    // different direction is a distinct state, so the square-corner constraint
    // below can route a tile a second way instead of dead-ending on the first.
    const posOf = (x: number, y: number) => `${x},${y}`;
    const stateOf = (x: number, y: number, dir: string | null) => `${x},${y},${dir ?? '_'}`;

    // Open set as an array (linear extract-min) plus a state->node map for O(1)
    // lookup. The (x,y,dir) state quadruples the search vs a plain tile A*, so
    // the old sort-every-pop + linear find would make long drags janky.
    const startNode: AStarNode = {
      pos: start,
      g: 0,
      h: this.heuristic(start, end),
      f: this.heuristic(start, end),
      parent: null,
      fromDir: null,
      prevTurnDir: null,
    };
    const openSet: AStarNode[] = [startNode];
    const openMap = new Map<string, AStarNode>([[stateOf(start.x, start.y, null), startNode]]);
    const closedSet = new Set<string>();

    const tileSkipsSegment = (t: TrackData | undefined): boolean =>
      !!(t && (t.type === 'signal' || t.signal_state || t.type === 'station' || t.station_id));

    while (openSet.length > 0) {
      // Extract-min by linear scan, then swap-remove from the array.
      let minIdx = 0;
      for (let i = 1; i < openSet.length; i++) {
        if (openSet[i].f < openSet[minIdx].f) minIdx = i;
      }
      const current = openSet[minIdx];
      openSet[minIdx] = openSet[openSet.length - 1];
      openSet.pop();
      const posKey = posOf(current.pos.x, current.pos.y);
      const stateKey = stateOf(current.pos.x, current.pos.y, current.fromDir);
      openMap.delete(stateKey);

      if (current.pos.x === end.x && current.pos.y === end.y) {
        // The end tile lays a connecting segment too; reject this arrival if it
        // would meet the penultimate tile at a 90° corner. Other arrivals (a
        // different incoming direction) remain in the open set to be tried.
        if (current.parent && current.fromDir !== null) {
          const endTile = trackMap.get(posKey);
          const penTile = trackMap.get(posOf(current.parent.pos.x, current.parent.pos.y));
          if (!tileSkipsSegment(endTile) && !tileSkipsSegment(penTile)) {
            const endAngle = this.endTileScreenAngle(
              options.forcedEndSegment ?? null,
              current.fromDir
            );
            const penAngle =
              current.parent.fromDir === null
                ? this.startTileScreenAngle(
                    trackMap,
                    current.parent.pos.x,
                    current.parent.pos.y,
                    options.forcedStartSegment ?? null,
                    current.fromDir
                  )
                : this.tileScreenAngle(current.parent.fromDir, current.fromDir);
            if (this.isSquareCornerAngle(penAngle, endAngle)) {
              closedSet.add(stateKey);
              continue;
            }
          }
        }

        const path = this.reconstructPath(current);
        // alignZigzagTails inserts a tile adjacent to the end to make
        // zigzag endings look smooth; that insertion ignores R/T
        // constraints and can also break adjacency, so skip it when
        // either endpoint is locked.
        if (startEdges || endEdges) return path;
        return this.alignZigzagTails(path, end);
      }

      closedSet.add(stateKey);

      const neighbors = [
        { x: current.pos.x, y: current.pos.y + 1, dir: 'NORTH' },
        { x: current.pos.x + 1, y: current.pos.y, dir: 'EAST' },
        { x: current.pos.x, y: current.pos.y - 1, dir: 'SOUTH' },
        { x: current.pos.x - 1, y: current.pos.y, dir: 'WEST' },
      ];

      for (const neighbor of neighbors) {
        // Outside the bounded search region — keeps the search finite.
        if (
          neighbor.x < boundMinX ||
          neighbor.x > boundMaxX ||
          neighbor.y < boundMinY ||
          neighbor.y > boundMaxY
        ) {
          continue;
        }

        const nPosKey = posOf(neighbor.x, neighbor.y);
        // closedSet is keyed on (tile, incoming direction); the neighbor would
        // be entered moving neighbor.dir.
        if (closedSet.has(stateOf(neighbor.x, neighbor.y, neighbor.dir))) continue;

        const existingTrack = trackMap.get(nPosKey);
        const entryDir = this.getOppositeDir(neighbor.dir);

        // Forced start segment: the first move out of the start
        // tile must exit through one of the forced segment's edges.
        if (startEdges && current.fromDir === null && !startEdges.includes(neighbor.dir)) {
          continue;
        }

        // Forced end segment: the move that lands on the end tile
        // must enter through one of the forced end segment's edges
        // (i.e., the entry edge of the end tile must be in endEdges).
        if (
          endEdges &&
          neighbor.x === end.x &&
          neighbor.y === end.y &&
          !endEdges.includes(entryDir)
        ) {
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

        // Square-corner avoidance: leaving `current` toward this neighbor fixes
        // current's exit edge and therefore the screen angle of the segment it
        // lays. Forbid the move if that segment would meet the previous tile's
        // at a right angle — A* must instead ease the turn through a 45° NS/EW
        // diagonal. Tiles that lay no segment (signals/stations) are exempt.
        if (current.parent && current.fromDir !== null) {
          const curTile = trackMap.get(posKey);
          const parentTile = trackMap.get(posOf(current.parent.pos.x, current.parent.pos.y));
          if (!tileSkipsSegment(curTile) && !tileSkipsSegment(parentTile)) {
            const currentAngle = this.tileScreenAngle(current.fromDir, neighbor.dir);
            const parentAngle =
              current.parent.fromDir === null
                ? this.startTileScreenAngle(
                    trackMap,
                    current.parent.pos.x,
                    current.parent.pos.y,
                    options.forcedStartSegment ?? null,
                    current.fromDir
                  )
                : this.tileScreenAngle(current.parent.fromDir, current.fromDir);
            if (this.isSquareCornerAngle(parentAngle, currentAngle)) {
              continue;
            }
          }
        }

        // No self-intersection: a single drag must not cross its own tiles.
        // Direction-state search can otherwise loop a tile back over an earlier
        // one, which renders as a multi-segment switch (and hid a corner). If
        // the only route would self-cross, A* finds none and the caller treats
        // the build as not-yet-valid.
        let ancestorCrosses = false;
        for (let a: AStarNode | null = current; a; a = a.parent) {
          if (a.pos.x === neighbor.x && a.pos.y === neighbor.y) {
            ancestorCrosses = true;
            break;
          }
        }
        if (ancestorCrosses) continue;

        let moveCost = costs.straight;
        let turnDir: string | null = null;

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
          const currentTileTrack = trackMap.get(posKey);
          const currentBits = currentTileTrack ? currentTileTrack.track_bits || 0 : 0;
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

        const neighborStateKey = stateOf(neighbor.x, neighbor.y, neighbor.dir);
        const existing = openMap.get(neighborStateKey);
        if (existing) {
          if (g < existing.g) {
            existing.g = g;
            existing.f = f;
            existing.parent = current;
            existing.fromDir = neighbor.dir;
            existing.prevTurnDir = turnDir || current.prevTurnDir;
          }
        } else {
          const node: AStarNode = {
            pos: { x: neighbor.x, y: neighbor.y },
            g,
            h,
            f,
            parent: current,
            fromDir: neighbor.dir,
            prevTurnDir: turnDir || current.prevTurnDir,
          };
          openSet.push(node);
          openMap.set(neighborStateKey, node);
        }
      }
    }

    // A* exhausted without an eased route. The diagonalLine fallback ignores
    // existing track, so validate it: if it would lay a 90° corner (e.g. a
    // forced start segment perpendicular to an adjacent target), there is no
    // valid build — return null so the preview stays invalid.
    const fallback = this.diagonalLine(start, end);
    if (
      this.pathHasSquareCorner(
        fallback,
        trackMap,
        options.forcedStartSegment ?? null,
        options.forcedEndSegment ?? null
      )
    ) {
      return null;
    }
    return fallback;
  }

  heuristic(a: Point, b: Point): number {
    return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y));
  }

  isReverse(dir1: string, dir2: string): boolean {
    const opposites: Record<string, string> = {
      NORTH: 'SOUTH',
      SOUTH: 'NORTH',
      EAST: 'WEST',
      WEST: 'EAST',
    };
    return opposites[dir1] === dir2;
  }

  getOppositeDir(dir: string): string {
    const opposites: Record<string, string> = {
      NORTH: 'SOUTH',
      SOUTH: 'NORTH',
      EAST: 'WEST',
      WEST: 'EAST',
    };
    return opposites[dir];
  }

  isStationEndpoint(x: number, y: number, stationTiles: TrackData[]): boolean {
    let adjacentStationCount = 0;
    const deltas = [
      { dx: 0, dy: 1 },
      { dx: 0, dy: -1 },
      { dx: 1, dy: 0 },
      { dx: -1, dy: 0 },
    ];

    for (const d of deltas) {
      const hasAdjacentStation = stationTiles.some((t) => t.x === x + d.dx && t.y === y + d.dy);
      if (hasAdjacentStation) adjacentStationCount++;
    }

    return adjacentStationCount <= 1;
  }

  getTurnDirection(fromDir: string, toDir: string): string | null {
    const turns: Record<string, Record<string, string>> = {
      NORTH: { EAST: 'RIGHT', WEST: 'LEFT' },
      EAST: { SOUTH: 'RIGHT', NORTH: 'LEFT' },
      SOUTH: { WEST: 'RIGHT', EAST: 'LEFT' },
      WEST: { NORTH: 'RIGHT', SOUTH: 'LEFT' },
    };
    if (!turns[fromDir]) {
      return null;
    }
    return turns[fromDir][toDir] || null;
  }

  // --- 90°-corner avoidance (octilinear easing) -----------------------------
  // The render grid is rotated 45°, so each laid segment draws as one of four
  // screen lines: NW/SE => 0° (horizontal), NS => 45° (╱), NE/SW => 90°
  // (vertical), EW => 135° (╲). Two adjacent path tiles whose segments differ
  // by 90° render as a "square corner" — the same shape isSquareCornerSwitch
  // rejects inside one tile (see constants.ts). A* forbids it across tiles too,
  // so every direction change is eased by a 45° NS/EW diagonal instead.

  // Screen angle (0/45/90/135) of the segment joining two tile edges.
  segScreenAngle(edgeA: string, edgeB: string): number {
    const has = (a: string, b: string) =>
      (edgeA === a && edgeB === b) || (edgeA === b && edgeB === a);
    if (has('NORTH', 'SOUTH')) return 45; // NS ╱
    if (has('EAST', 'WEST')) return 135; // EW ╲
    if (has('NORTH', 'EAST') || has('SOUTH', 'WEST')) return 90; // NE/SW vertical
    return 0; // NW or SE horizontal
  }

  // Do two screen angles meet at a right angle? (0↔90 or 45↔135.)
  isSquareCornerAngle(a: number, b: number): boolean {
    let d = Math.abs(a - b);
    if (d > 90) d = 180 - d;
    return d === 90;
  }

  // Angle of an interior tile entered by moving inDir and left by moving
  // outDir: entry edge is opposite the arrival move, exit edge is the
  // departure move (mirrors pathToTracks' prev&&next branch).
  tileScreenAngle(inDir: string, outDir: string): number {
    return this.segScreenAngle(this.getOppositeDir(inDir), outDir);
  }

  // The existing-track port that binds to an exit edge — mirror of the
  // getConnectedPort helper inside pathToTracks' !prev&&next branch.
  bindPortToExit(bits: number, exit: string): string | null {
    if (bits & 1 && (exit === 'NORTH' || exit === 'SOUTH')) return exit === 'NORTH' ? 'SOUTH' : 'NORTH';
    if (bits & 2 && (exit === 'EAST' || exit === 'WEST')) return exit === 'EAST' ? 'WEST' : 'EAST';
    if (bits & 4 && (exit === 'NORTH' || exit === 'EAST')) return exit === 'NORTH' ? 'EAST' : 'NORTH';
    if (bits & 8 && (exit === 'NORTH' || exit === 'WEST')) return exit === 'NORTH' ? 'WEST' : 'NORTH';
    if (bits & 16 && (exit === 'SOUTH' || exit === 'EAST')) return exit === 'SOUTH' ? 'EAST' : 'SOUTH';
    if (bits & 32 && (exit === 'SOUTH' || exit === 'WEST')) return exit === 'SOUTH' ? 'WEST' : 'SOUTH';
    return null;
  }

  // Angle of the START tile's connecting segment when the path leaves via
  // outDir. Mirrors pathToTracks' !prev&&next branch: a forced segment, else
  // the existing track's port that binds to outDir, else the default
  // opposite-edge stub (always a 45° diagonal, which never corners).
  startTileScreenAngle(
    trackMap: Map<string, TrackData>,
    x: number,
    y: number,
    forcedStart: string | null,
    outDir: string
  ): number {
    if (forcedStart && FORCED_SEGMENT_PORTS[forcedStart]) {
      const [p1, p2] = FORCED_SEGMENT_PORTS[forcedStart];
      return this.segScreenAngle(p1, p2);
    }
    const existing = trackMap.get(`${x},${y}`);
    if (existing && existing.track_bits) {
      const port = this.bindPortToExit(existing.track_bits, outDir);
      if (port) return this.segScreenAngle(port, outDir);
    }
    return this.segScreenAngle(this.getOppositeDir(outDir), outDir);
  }

  // Angle of the END tile's connecting segment, arrived at by moving arriveDir.
  // Mirrors pathToTracks' prev&&!next branch's forced/default cases (the
  // opposite-edge stub is a 45° diagonal).
  endTileScreenAngle(forcedEnd: string | null, arriveDir: string): number {
    if (forcedEnd && FORCED_SEGMENT_PORTS[forcedEnd]) {
      const [p1, p2] = FORCED_SEGMENT_PORTS[forcedEnd];
      return this.segScreenAngle(p1, p2);
    }
    const entryEdge = this.getOppositeDir(arriveDir);
    return this.segScreenAngle(entryEdge, this.getOppositeDir(entryEdge));
  }

  // Does a finished path lay any two adjacent tiles at a 90° screen corner?
  // Uses each tile's path-local segment (in/out moves, or the junction segment
  // at the ends), so it is the ground-truth gate for fallbacks that the
  // constrained search does not produce (e.g. diagonalLine). Tiles that lay no
  // segment (signals/stations) break the run, matching pathToTracks.
  pathHasSquareCorner(
    path: Point[],
    trackMap: Map<string, TrackData>,
    forcedStart: string | null,
    forcedEnd: string | null
  ): boolean {
    let prevAngle: number | null = null;
    for (let i = 0; i < path.length; i++) {
      const p = path[i];
      const tile = trackMap.get(`${p.x},${p.y}`);
      if (tile && (tile.type === 'signal' || tile.signal_state || tile.type === 'station' || tile.station_id)) {
        prevAngle = null;
        continue;
      }
      const prev = i > 0 ? path[i - 1] : null;
      const next = i < path.length - 1 ? path[i + 1] : null;
      let angle: number;
      if (prev && next) {
        angle = this.tileScreenAngle(getDirection(prev, p)!, getDirection(p, next)!);
      } else if (next) {
        angle = this.startTileScreenAngle(trackMap, p.x, p.y, forcedStart, getDirection(p, next)!);
      } else if (prev) {
        angle = this.endTileScreenAngle(forcedEnd, getDirection(prev, p)!);
      } else {
        continue;
      }
      if (prevAngle !== null && this.isSquareCornerAngle(prevAngle, angle)) return true;
      prevAngle = angle;
    }
    return false;
  }

  reconstructPath(node: AStarNode): Point[] {
    const path: Point[] = [];
    let current: AStarNode | null = node;
    while (current) {
      path.unshift(current.pos);
      current = current.parent;
    }
    return path;
  }

  diagonalLine(start: Point, end: Point): Point[] {
    const path = [{ x: start.x, y: start.y }];
    let x = start.x;
    let y = start.y;
    const dx = end.x > start.x ? 1 : end.x < start.x ? -1 : 0;
    const dy = end.y > start.y ? 1 : end.y < start.y ? -1 : 0;

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

  detectZigzagPattern(path: Point[]): string[] | null {
    if (path.length < 4) return null;

    const directions: string[] = [];
    for (let i = 1; i < path.length; i++) {
      const dx = path[i].x - path[i - 1].x;
      const dy = path[i].y - path[i - 1].y;
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
      if (middle[i] === middle[i - 1]) alternates = false;
    }

    return alternates ? uniqueDirs : null;
  }

  alignZigzagTails(path: Point[], end: Point): Point[] {
    if (path.length < 3) return path;

    const pattern = this.detectZigzagPattern(path);
    if (!pattern) return path;

    const result = [...path];

    if (result.length >= 2) {
      const firstMove = {
        dx: result[1].x - result[0].x,
        dy: result[1].y - result[0].y,
      };
      const firstDir =
        firstMove.dy > 0
          ? 'NORTH'
          : firstMove.dy < 0
            ? 'SOUTH'
            : firstMove.dx > 0
              ? 'EAST'
              : 'WEST';

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
        dy: result[lastIdx].y - result[lastIdx - 1].y,
      };
      const lastDir =
        lastMove.dy > 0
          ? 'NORTH'
          : lastMove.dy < 0
            ? 'SOUTH'
            : lastMove.dx > 0
              ? 'EAST'
              : 'WEST';

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

        if (
          (insertPos.x !== prevPos.x || insertPos.y !== prevPos.y) &&
          (insertPos.x !== end.x || insertPos.y !== end.y)
        ) {
          result.splice(lastIdx, 0, insertPos);
        }
      }
    }

    return result;
  }

  getDelta(dir: string): { dx: number; dy: number } {
    switch (dir) {
      case 'NORTH':
        return { dx: 0, dy: 1 };
      case 'SOUTH':
        return { dx: 0, dy: -1 };
      case 'EAST':
        return { dx: 1, dy: 0 };
      case 'WEST':
        return { dx: -1, dy: 0 };
      default:
        return { dx: 0, dy: 0 };
    }
  }
}

// Forced-segment shape -> (port1, port2). When R/T are used during a drag,
// the editor passes the chosen name in options.forcedStartSegment /
// forcedEndSegment and we lay exactly that segment on the start/end tile.
export const FORCED_SEGMENT_PORTS: Record<string, [string, string]> = {
  NS: ['NORTH', 'SOUTH'],
  EW: ['EAST', 'WEST'],
  NE: ['NORTH', 'EAST'],
  NW: ['NORTH', 'WEST'],
  SE: ['SOUTH', 'EAST'],
  SW: ['SOUTH', 'WEST'],
};

interface BuildTrack {
  x: number;
  y: number;
  type: string;
  directed_paths: DirectedPath[];
  track_bits?: number;
}

export function pathToTracks(
  path: Point[] | null | undefined,
  cachedNetwork: { tracks?: TrackData[] } | null | undefined,
  options: PathToTracksOptions = {}
): BuildTrack[] {
  if (!path || path.length === 0) return [];

  const avoidConnect = options.avoidConnect === true;
  const forcedStart = options.forcedStartSegment || null;
  const forcedEnd = options.forcedEndSegment || null;

  const tracksToUpdate = new Map<string, BuildTrack>();
  const existingTracks = new Map<string, TrackData>();

  if (cachedNetwork && cachedNetwork.tracks) {
    for (const track of cachedNetwork.tracks) {
      existingTracks.set(`${track.x},${track.y}`, track);
    }
  }

  function neighborHasPortTo(neighborX: number, neighborY: number, portName: string): boolean {
    const neighborTrack = existingTracks.get(`${neighborX},${neighborY}`);
    if (!neighborTrack) return false;

    if (neighborTrack.directed_paths) {
      return neighborTrack.directed_paths.some(
        (p) => p.port1 === portName || p.port2 === portName
      );
    }
    if (neighborTrack.paths) {
      return neighborTrack.paths.some((p) => p.includes(portName));
    }
    if (neighborTrack.connections) {
      return portName in neighborTrack.connections;
    }
    return false;
  }

  function addPath(
    x: number,
    y: number,
    port1: string | null,
    port2: string | null,
    direction = 'both'
  ): void {
    const key = `${x},${y}`;
    if (!tracksToUpdate.has(key)) {
      const existing = existingTracks.get(key);
      const existingType = existing ? existing.type : null;
      tracksToUpdate.set(key, {
        x,
        y,
        type: existingType || 'STRAIGHT',
        directed_paths: [],
      });
    }
    const track = tracksToUpdate.get(key)!;

    const pathExists = track.directed_paths.some(
      (p) =>
        (p.port1 === port1 && p.port2 === port2) || (p.port1 === port2 && p.port2 === port1)
    );
    if (!pathExists && port1 !== port2) {
      track.directed_paths.push({ port1, port2, direction });
    }
  }

  for (let i = 0; i < path.length; i++) {
    const current = path[i];

    const existingForProtection = existingTracks.get(`${current.x},${current.y}`);
    if (
      existingForProtection &&
      (existingForProtection.type === 'station' || existingForProtection.station_id)
    ) {
      continue;
    }
    if (
      existingForProtection &&
      (existingForProtection.type === 'signal' || existingForProtection.signal_state)
    ) {
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
        const getConnectedPort = (bits: number, port: string | null): string | null => {
          if (bits & 1 && (port === 'NORTH' || port === 'SOUTH'))
            return port === 'NORTH' ? 'SOUTH' : 'NORTH';
          if (bits & 2 && (port === 'EAST' || port === 'WEST'))
            return port === 'EAST' ? 'WEST' : 'EAST';
          if (bits & 4 && (port === 'NORTH' || port === 'EAST'))
            return port === 'NORTH' ? 'EAST' : 'NORTH';
          if (bits & 8 && (port === 'NORTH' || port === 'WEST'))
            return port === 'NORTH' ? 'WEST' : 'NORTH';
          if (bits & 16 && (port === 'SOUTH' || port === 'EAST'))
            return port === 'SOUTH' ? 'EAST' : 'SOUTH';
          if (bits & 32 && (port === 'SOUTH' || port === 'WEST'))
            return port === 'SOUTH' ? 'WEST' : 'SOUTH';
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
          { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' },
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
        const endBits = endTrack ? endTrack.track_bits || 0 : 0;
        const avoidExit = computeNonConflictingExit(endBits, entryDir);
        addPath(current.x, current.y, entryDir, avoidExit, 'both');
        continue;
      }

      const neighbors = [
        { dx: 0, dy: 1, myPort: 'NORTH', theirPort: 'SOUTH' },
        { dx: 0, dy: -1, myPort: 'SOUTH', theirPort: 'NORTH' },
        { dx: 1, dy: 0, myPort: 'EAST', theirPort: 'WEST' },
        { dx: -1, dy: 0, myPort: 'WEST', theirPort: 'EAST' },
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

  const tracks: BuildTrack[] = [];
  for (const track of tracksToUpdate.values()) {
    const trackBits = pathsToTrackBits(track.directed_paths);

    if (FORBIDDEN_TRACK_BITS.has(trackBits)) {
      console.warn(
        `[pathToTracks] Skipping forbidden configuration (${trackBits}) at (${track.x}, ${track.y})`
      );
      const w = window as unknown as {
        showError?: unknown;
        showToast?: (message: string, kind?: string, durationMs?: number) => void;
      };
      if (w.showError) {
        w.showToast?.(
          `Cannot create forbidden configuration (${trackBits}). This would require a 3-way switch.`,
          'error',
          5000
        );
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
export function computeNonConflictingExit(
  existingBits: number,
  entryDir: string | null
): string | null {
  const opposite = getOpposite(entryDir);
  if (!existingBits) return opposite;

  const used = new Set<string>();
  if (existingBits & 1) {
    used.add('NORTH');
    used.add('SOUTH');
  }
  if (existingBits & 2) {
    used.add('EAST');
    used.add('WEST');
  }
  if (existingBits & 4) {
    used.add('NORTH');
    used.add('EAST');
  }
  if (existingBits & 8) {
    used.add('NORTH');
    used.add('WEST');
  }
  if (existingBits & 16) {
    used.add('SOUTH');
    used.add('EAST');
  }
  if (existingBits & 32) {
    used.add('SOUTH');
    used.add('WEST');
  }

  if (entryDir !== null && used.has(entryDir)) return opposite;

  for (const dir of ['NORTH', 'EAST', 'SOUTH', 'WEST']) {
    if (dir !== entryDir && !used.has(dir)) return dir;
  }
  return opposite;
}

export function getDirection(from: Point, to: Point): string | null {
  if (to.y > from.y) return 'NORTH';
  if (to.y < from.y) return 'SOUTH';
  if (to.x > from.x) return 'EAST';
  if (to.x < from.x) return 'WEST';
  return null;
}

export function getOpposite(dir: string | null): string | null {
  switch (dir) {
    case 'NORTH':
      return 'SOUTH';
    case 'SOUTH':
      return 'NORTH';
    case 'EAST':
      return 'WEST';
    case 'WEST':
      return 'EAST';
    default:
      return null;
  }
}

// ============================================================================
// Signal Placement Validation
// ============================================================================

export const VALID_DETECTION_DIRECTIONS: Record<number, string[]> = {
  1: ['NORTH', 'SOUTH'],
  2: ['EAST', 'WEST'],
  4: ['NORTH', 'EAST'],
  8: ['NORTH', 'WEST'],
  16: ['SOUTH', 'EAST'],
  32: ['SOUTH', 'WEST'],
};

export const SWITCH_TRACK_BITS = new Set<number>([
  3, 24, 36, 5, 9, 17, 33, 6, 10, 18, 34, 12, 20, 40, 48, 7, 11, 19, 35, 14, 38, 26, 50, 21, 37,
  25, 41,
]);

export function getValidSignalDirections(trackBits: number): string[] {
  if (SWITCH_TRACK_BITS.has(trackBits)) {
    return [];
  }
  return VALID_DETECTION_DIRECTIONS[trackBits] || [];
}

// ============================================================================
// Block Assignment Validation
// ============================================================================

export function isCrossingForBlock(trackBits: number): boolean {
  return trackBits === 3 || trackBits === 24 || trackBits === 36;
}

export function validateBlockTile(track: TrackData | null | undefined): {
  valid: boolean;
  reason: string | null;
} {
  if (!track) {
    return { valid: false, reason: 'No track at this position' };
  }

  if (track.is_switch) {
    return { valid: false, reason: 'Switches cannot be inside a block' };
  }

  if (isCrossingForBlock(track.track_bits || 0)) {
    return { valid: false, reason: 'Crossings cannot be inside a block - paths are isolated' };
  }

  return { valid: true, reason: null };
}

export function validateBlockPath(
  path: Point[],
  tracks: TrackData[] | null | undefined,
  mode = 'create'
): {
  valid: boolean;
  warnings: string[];
  errors: string[];
  blocksInPath: string[];
  signalsInPath: Array<{ x: number; y: number; track: TrackData }>;
} {
  const warnings: string[] = [];
  const errors: string[] = [];
  const blocksInPath = new Set<string>();
  const signalsInPath: Array<{ x: number; y: number; track: TrackData }> = [];
  const trackMap = new Map<string, TrackData>();

  if (tracks) {
    tracks.forEach((t) => trackMap.set(`${t.x},${t.y}`, t));
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
    signalsInPath,
  };
}
