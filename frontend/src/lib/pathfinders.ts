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
import { graphSearch, type SearchStep } from './graph-search';

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
  move: number;
  curve: number;
  createSwitch: number;
  createCrossover: number;
  createParallel: number;
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

// A node for the follow-existing search: a tile plus the edge it was entered
// through ('' for a seed, where every exposed edge is a valid first exit).
export interface TraceNode {
  x: number;
  y: number;
  entry: string;
}

export const traceNodeKey = (n: TraceNode): string => `${n.x},${n.y}|${n.entry}`;

// A node for the placement search (laying new track): a tile, the direction the
// path arrived by (null at the seed), the 2-tile-window heading at this tile
// (grandparent -> here, for the curve penalty; null at the seed), and a parent
// link so the neighbor rules can read ancestry (the no-90° corner check and the
// no-self-intersection check both walk it).
interface PlacementNode {
  pos: Point;
  fromDir: string | null;
  heading: string | null;
  parent: PlacementNode | null;
}

// State = tile + arrival direction + 2-tile heading. The heading is part of the
// state because two paths reaching the same tile by the same last step but with
// different prior steps (e.g. a straight vs a diagonal heading) face different
// curve costs onward, so they must stay distinct in the closed set.
const placementNodeKey = (n: PlacementNode): string =>
  `${n.pos.x},${n.pos.y},${n.fromDir ?? '_'},${n.heading ?? '_'}`;

interface PlacementContext {
  trackMap: Map<string, TrackData>;
  costs: CostTable;
  preferredDirs: string[];
  startEdges: string[] | null;
  endEdges: string[] | null;
  end: Point;
  boundMinX: number;
  boundMaxX: number;
  boundMinY: number;
  boundMaxY: number;
  forcedStartSegment: string | null;
  tileSkipsSegment: (t: TrackData | undefined) => boolean;
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
    // Simutrans-style cost model. A "move" is one tile step; a straight step and
    // a diagonal (staircase) step cost the SAME base, and direction is taxed
    // separately by `curve`, measured over a 2-tile window (see
    // placementNeighbors / heuristic). So a clean straight run and a clean 45°
    // diagonal are both cheap, and only the transitions between headings cost.
    this.costs = {
      useExisting: 1, // step along existing track (path of least resistance)
      extend: 2, // first step extending the start tile's own track
      move: 3, // any step on empty ground — straight or diagonal alike
      curve: 5, // per 2-tile-window heading change (the straightness lever)
      createSwitch: 8, // merging the new track into existing track as a switch
      createCrossover: 12, // crossing existing track perpendicularly
      // A parallel crossing (NE|SW or NW|SE over a track that holds the other
      // half) is a benign double-track, not a junction: a minimal surcharge so
      // the router lays it almost as freely as a plain move.
      createParallel: 1,
      reverse: 100, // 180° about-face — effectively forbidden
    };

    // Avoid-connect mode (shift key): same move/curve model as normal, but the
    // moves that CONNECT to existing track are penalised so the new track stays
    // independent — reusing an existing edge (useExisting), extending an existing
    // segment (extend), and merging into a switch (createSwitch, penalised the
    // hardest). The INDEPENDENT overlaps are not connections: createParallel (a
    // parallel double-track) is free, and createCrossover (a perpendicular
    // crossing) keeps its normal cost. Because merging costs far more than a
    // crossing, a drag forced through existing track lays a crossing rather than
    // a switch, and the router never detours out of its way to seek one.
    this.avoidConnectCosts = {
      useExisting: 500,
      extend: 500,
      move: 3, // same as normal
      curve: 5, // same as normal
      createSwitch: 2000, // never merge into a switch
      createCrossover: 12, // normal cost — an independent overlap, not a connection
      createParallel: 0, // free — a benign independent double-track
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

    // Two segments that share no edge make a multi-path tile. They split by how
    // they sit on the 45°-rotated screen:
    //   - parallel: same screen angle, offset, never touching — NE|SW (both
    //     vertical) and NW|SE (both horizontal). Laying one over the other is a
    //     benign double-track.
    //   - crossover: perpendicular, meeting at the tile centre — NS (╱) + EW (╲).
    const matches = (a: number, b: number) =>
      (existingBits & a && newBit & b) || (existingBits & b && newBit & a);

    const parallelPairs = [
      [this.TRACK_NE, this.TRACK_SW],
      [this.TRACK_NW, this.TRACK_SE],
    ];
    for (const [a, b] of parallelPairs) {
      if (combined & a && combined & b && matches(a, b)) return 'parallel';
    }

    if (combined & this.TRACK_NS && combined & this.TRACK_EW && matches(this.TRACK_NS, this.TRACK_EW)) {
      return 'crossover';
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

  // Neighbor plug for graphSearch over EXISTING track only (trace, reachable
  // preview, and — with the stop knobs — block/platform selection). It never
  // steps onto an empty tile: from a seed (entry '') every exposed edge is a
  // valid first exit; afterwards the entry edge constrains exits to the
  // segment(s) that bind it (getExitsForEntry), so a path cannot cheat across
  // the disconnected segments of a switch. A neighbour must carry track on the
  // facing edge to count as connected. Cost is 1 per hop (shortest = fewest
  // tiles). With stopAtSwitches / stopAtCrossings the walk refuses to enter (or
  // leave) switch / crossing tiles — used by block/platform, which cannot
  // contain them (see validateBlockTile).
  followExistingNeighbors(
    trackByKey: Map<string, TrackData>,
    opts: { stopAtSwitches?: boolean; stopAtCrossings?: boolean } = {}
  ): (node: TraceNode) => SearchStep<TraceNode>[] {
    const STEP: Record<string, [number, number]> = {
      NORTH: [0, 1], SOUTH: [0, -1], EAST: [1, 0], WEST: [-1, 0],
    };
    const OPP: Record<string, string> = {
      NORTH: 'SOUTH', SOUTH: 'NORTH', EAST: 'WEST', WEST: 'EAST',
    };
    return (node: TraceNode): SearchStep<TraceNode>[] => {
      const here = trackByKey.get(`${node.x},${node.y}`);
      const bits = here?.track_bits ?? 0;
      if (opts.stopAtSwitches && here?.is_switch) return [];
      if (opts.stopAtCrossings && isCrossingForBlock(bits)) return [];
      const exits =
        node.entry === ''
          ? this.getExistingTrackDirections(bits)
          : this.getExitsForEntry(bits, node.entry);
      const out: SearchStep<TraceNode>[] = [];
      for (const exit of exits) {
        const step = STEP[exit];
        if (!step) continue;
        const nx = node.x + step[0];
        const ny = node.y + step[1];
        const nTrack = trackByKey.get(`${nx},${ny}`);
        if (!nTrack) continue;
        const back = OPP[exit];
        if (!this.getExistingTrackDirections(nTrack.track_bits ?? 0).includes(back)) continue;
        if (opts.stopAtSwitches && nTrack.is_switch) continue;
        if (opts.stopAtCrossings && isCrossingForBlock(nTrack.track_bits ?? 0)) continue;
        out.push({ node: { x: nx, y: ny, entry: back }, cost: 1 });
      }
      return out;
    };
  }

  // Neighbor plug for graphSearch when LAYING new track (the track tool). Unlike
  // followExistingNeighbors it may step onto empty tiles; it carries the full
  // construction rule set — the cost table, segment-binding on existing track,
  // the no-90° corner rule, the no-self-intersection rule, the bounded search
  // box, and the forced start/end edges. Returns the valid successors in
  // N→E→S→W order (the order the search settles ties by). The end tile is never
  // expanded past — findPath's goal check owns the end.
  placementNeighbors(ctx: PlacementContext): (node: PlacementNode) => SearchStep<PlacementNode>[] {
    return (current: PlacementNode): SearchStep<PlacementNode>[] => {
      const out: SearchStep<PlacementNode>[] = [];
      if (current.pos.x === ctx.end.x && current.pos.y === ctx.end.y) return out;

      const posKey = `${current.pos.x},${current.pos.y}`;
      const neighbors = [
        { x: current.pos.x, y: current.pos.y + 1, dir: 'NORTH' },
        { x: current.pos.x + 1, y: current.pos.y, dir: 'EAST' },
        { x: current.pos.x, y: current.pos.y - 1, dir: 'SOUTH' },
        { x: current.pos.x - 1, y: current.pos.y, dir: 'WEST' },
      ];

      for (const neighbor of neighbors) {
        if (
          neighbor.x < ctx.boundMinX ||
          neighbor.x > ctx.boundMaxX ||
          neighbor.y < ctx.boundMinY ||
          neighbor.y > ctx.boundMaxY
        ) {
          continue;
        }

        const nPosKey = `${neighbor.x},${neighbor.y}`;
        const existingTrack = ctx.trackMap.get(nPosKey);
        const entryDir = this.getOppositeDir(neighbor.dir);

        if (ctx.startEdges && current.fromDir === null && !ctx.startEdges.includes(neighbor.dir)) {
          continue;
        }

        if (
          ctx.endEdges &&
          neighbor.x === ctx.end.x &&
          neighbor.y === ctx.end.y &&
          !ctx.endEdges.includes(entryDir)
        ) {
          continue;
        }

        if (existingTrack && (existingTrack.type === 'signal' || existingTrack.signal_state)) {
          const signalPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
          if (!signalPorts.includes(entryDir)) continue;
        }

        if (existingTrack && (existingTrack.type === 'station' || existingTrack.station_id)) {
          const stationPorts = this.getExistingTrackDirections(existingTrack.track_bits || 0);
          if (!stationPorts.includes(entryDir)) continue;
        }

        // A platform/signal keeps its own rail, so the new track must join that
        // rail rather than an edge it doesn't serve. Passing THROUGH one (we have
        // an entry), the exit must be the paired edge of the segment we entered
        // on — follow the rail straight through, no turning inside. Starting ON
        // one (no entry yet), the first move must leave via one of its rail
        // ports, so the new track connects to the rail instead of dangling.
        const curSkip = ctx.trackMap.get(posKey);
        if (curSkip && ctx.tileSkipsSegment(curSkip)) {
          if (current.fromDir !== null) {
            const entryEdgeHere = this.getOppositeDir(current.fromDir);
            const railExits = this.getExitsForEntry(curSkip.track_bits || 0, entryEdgeHere);
            if (!railExits.includes(neighbor.dir)) continue;
          } else {
            const startPorts = this.getExistingTrackDirections(curSkip.track_bits || 0);
            if (!startPorts.includes(neighbor.dir)) continue;
          }
        }

        if (current.parent && current.fromDir !== null) {
          // No-90° check. With the rail-follow rule above, a skip tile's in/out
          // moves trace its own rail, so tileScreenAngle yields that rail's angle
          // here — platforms no longer need exempting from the corner check.
          const currentAngle = this.tileScreenAngle(current.fromDir, neighbor.dir);
          const parentAngle =
            current.parent.fromDir === null
              ? this.startTileScreenAngle(
                  ctx.trackMap,
                  current.parent.pos.x,
                  current.parent.pos.y,
                  ctx.forcedStartSegment,
                  current.fromDir
                )
              : this.tileScreenAngle(current.parent.fromDir, current.fromDir);
          if (this.isSquareCornerAngle(parentAngle, currentAngle)) continue;
        }

        let ancestorCrosses = false;
        for (let a: PlacementNode | null = current; a; a = a.parent) {
          if (a.pos.x === neighbor.x && a.pos.y === neighbor.y) {
            ancestorCrosses = true;
            break;
          }
        }
        if (ancestorCrosses) continue;

        // Base per-tile cost: cheapest along existing track, then extending the
        // start tile's own track, else a plain move on empty ground. A straight
        // step and a diagonal (staircase) step cost the SAME here — direction is
        // charged by the curve penalty below.
        let moveCost = ctx.costs.move;

        if (existingTrack && existingTrack.track_bits) {
          const trackDirs = this.getExistingTrackDirections(existingTrack.track_bits);
          if (trackDirs.includes(entryDir)) moveCost = ctx.costs.useExisting;
        } else if (!current.fromDir) {
          if (ctx.preferredDirs.length > 0 && ctx.preferredDirs.includes(neighbor.dir)) {
            moveCost = ctx.costs.extend;
          }
        } else if (this.isReverse(current.fromDir, neighbor.dir)) {
          moveCost = ctx.costs.reverse;
        }

        // Curve penalty: pay `curve` whenever the 2-tile-window heading changes.
        // current.heading is the heading arriving at `current` (grandparent ->
        // current); newHeading spans current.parent -> neighbor. A clean
        // staircase keeps a constant diagonal heading, so its steady-state turn
        // cost is zero — you only pay to enter or leave it. (A 90° heading flip
        // can't occur: the no-90° screen rule above rejects square corners first.)
        const refPos = current.parent ? current.parent.pos : current.pos;
        const newHeading = this.heading8(refPos, { x: neighbor.x, y: neighbor.y });
        if (current.heading !== null && newHeading !== null && current.heading !== newHeading) {
          moveCost += ctx.costs.curve;
        }

        if (current.fromDir) {
          const currentTileTrack = ctx.trackMap.get(posKey);
          const currentBits = currentTileTrack ? currentTileTrack.track_bits || 0 : 0;
          const newPathBit = this.getTrackBitForPath(current.fromDir, neighbor.dir);
          const switchType = this.detectSwitchType(currentBits, newPathBit);
          if (switchType === 'crossover') moveCost += ctx.costs.createCrossover;
          else if (switchType === 'parallel') moveCost += ctx.costs.createParallel;
          else if (switchType === 'switch') moveCost += ctx.costs.createSwitch;
        }

        out.push({
          node: {
            pos: { x: neighbor.x, y: neighbor.y },
            fromDir: neighbor.dir,
            heading: newHeading,
            parent: current,
          },
          cost: moveCost,
        });
      }
      return out;
    };
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
    // unreachable would otherwise fan the search across the plane forever. The
    // margin dwarfs any 45° easing detour a point-to-point draw needs.
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

    const tileSkipsSegment = (t: TrackData | undefined): boolean =>
      !!(t && (t.type === 'signal' || t.signal_state || t.type === 'station' || t.station_id));

    // The end tile lays a connecting segment too; an arrival is only a goal if
    // that segment would not meet the penultimate tile at a 90° corner. A bad
    // arrival is left non-goal so other arrivals (a different incoming
    // direction) can still be tried. A forced end segment is the user's explicit
    // choice, so the corner check is skipped when endEdges is set.
    const validEndArrival = (node: PlacementNode): boolean => {
      if (!endEdges && node.parent && node.fromDir !== null) {
        const endTile = trackMap.get(`${end.x},${end.y}`);
        // Cap angle to check against the penultimate tile: a platform end uses
        // its own rail (the segment the path joins — the one holding the entry
        // edge), so a path can't meet a platform at a 90° corner; any other end
        // uses its computed cap. A penultimate platform already traces its rail
        // (rail-follow rule), so the penAngle below is correct too.
        let endAngle: number | null;
        if (tileSkipsSegment(endTile)) {
          const entryEdge = this.getOppositeDir(node.fromDir);
          const railExits = this.getExitsForEntry(endTile?.track_bits || 0, entryEdge);
          endAngle = railExits.length ? this.segScreenAngle(entryEdge, railExits[0]) : null;
        } else if (options.avoidConnect && endTile && endTile.track_bits) {
          // Avoid-connect mode lays a non-conflicting segment over existing end
          // track (a crossover/parallel) rather than the default cap — mirror
          // pathToTracks' avoid-connect end branch (computeNonConflictingExit) so
          // the corner check sees the segment that will actually be laid.
          const entryEdge = this.getOppositeDir(node.fromDir);
          const avoidExit = computeNonConflictingExit(endTile.track_bits, entryEdge);
          endAngle = avoidExit ? this.segScreenAngle(entryEdge, avoidExit) : null;
        } else {
          endAngle = this.endTileScreenAngle(options.forcedEndSegment ?? null, node.fromDir);
        }
        if (endAngle !== null) {
          const penAngle =
            node.parent.fromDir === null
              ? this.startTileScreenAngle(
                  trackMap,
                  node.parent.pos.x,
                  node.parent.pos.y,
                  options.forcedStartSegment ?? null,
                  node.fromDir
                )
              : this.tileScreenAngle(node.parent.fromDir, node.fromDir);
          if (this.isSquareCornerAngle(penAngle, endAngle)) return false;
        }
      }
      return true;
    };

    const seed: PlacementNode = { pos: start, fromDir: null, heading: null, parent: null };
    const { path } = graphSearch<PlacementNode>({
      seeds: [seed],
      key: placementNodeKey,
      heuristic: (n) => this.heuristic(n.pos, end),
      neighbors: this.placementNeighbors({
        trackMap,
        costs,
        preferredDirs,
        startEdges,
        endEdges,
        end,
        boundMinX,
        boundMaxX,
        boundMinY,
        boundMaxY,
        forcedStartSegment: options.forcedStartSegment ?? null,
        tileSkipsSegment,
      }),
      goal: (n) => n.pos.x === end.x && n.pos.y === end.y && validEndArrival(n),
    });

    if (path) {
      const tiles = path.map((n) => ({ x: n.pos.x, y: n.pos.y }));
      // alignZigzagTails smooths zigzag endings but ignores R/T constraints and
      // can break adjacency, so skip it when either endpoint is locked.
      if (startEdges || endEdges) return tiles;
      return this.alignZigzagTails(tiles, end);
    }

    // A* exhausted without an eased route. The diagonalLine fallback ignores
    // existing track, so reject it if it would lay a 90° corner.
    const fallback = this.diagonalLine(start, end);
    // The straight-line fallback can't follow a platform's rail, so reject it if
    // it would thread through a platform/signal interior — there is no clean
    // build, and the caller shows the drag as invalid instead.
    const threadsSkipTile = fallback.some((p, i) => {
      if (i === 0 || i === fallback.length - 1) return false;
      return tileSkipsSegment(trackMap.get(`${p.x},${p.y}`));
    });
    if (
      threadsSkipTile ||
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
    // Chebyshev distance scaled by the base move cost, so the estimate tracks
    // the cost table (each remaining step costs at least one move). Curve
    // penalties are only ever added on top, so this never overestimates. Both
    // cost tables share the same move value, so the scale is valid in normal and
    // avoid-connect mode alike.
    return this.costs.move * Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y));
  }

  // 8-way heading from one tile to another by the sign of the delta (NORTH=+y,
  // EAST=+x). Used for the 2-tile-window curve penalty: a straight run keeps one
  // cardinal heading and a clean diagonal staircase keeps one diagonal heading,
  // so only the transitions between them are charged.
  heading8(from: Point, to: Point): string | null {
    const dx = Math.sign(to.x - from.x);
    const dy = Math.sign(to.y - from.y);
    if (dx === 0 && dy === 0) return null;
    return (dy > 0 ? 'N' : dy < 0 ? 'S' : '') + (dx > 0 ? 'E' : dx < 0 ? 'W' : '');
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
  // the existing track's port that binds to outDir, else the auto cap. The
  // auto cap now mirrors the next tile's piece (parallel to it), so it never
  // squares with that tile; we return the straight-stub angle here as a safe
  // stand-in, because the stub never squares with the next tile either — so
  // the no-90° gate reaches the same verdict whichever cap is laid.
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
  // Mirrors pathToTracks' prev&&!next branch. The auto cap mirrors the previous
  // tile's piece (parallel to it) and so never squares with it; we return the
  // straight-stub angle as a safe stand-in (same reasoning as
  // startTileScreenAngle above), so the no-90° gate's verdict is unchanged.
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

    // The head/tail smoothing inserts a tile next to result[0] / end, but the
    // inserted tile can land diagonally from its other neighbour, breaking the
    // cardinal-step chain — which renders as a disconnected track at that end.
    // If smoothing produced any non-adjacent step, discard it and keep the
    // original (always-cardinal) path: a slightly less-smooth end beats a
    // broken one.
    for (let i = 1; i < result.length; i++) {
      if (Math.abs(result[i].x - result[i - 1].x) + Math.abs(result[i].y - result[i - 1].y) !== 1) {
        return path;
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

// Screen angle of the piece that joins two tile edges. Mirrors the class
// method ConstructionPathfinder.segScreenAngle; kept module-level so the
// standalone pathToTracks below can pick end-tile pieces. Returns -1 for a
// missing/invalid edge pair so callers fall back to a straight stub.
function screenAngle(a: string | null, b: string | null): number {
  const has = (x: string, y: string) => (a === x && b === y) || (a === y && b === x);
  if (has('NORTH', 'SOUTH')) return 45; // NS ╱
  if (has('EAST', 'WEST')) return 135; // EW ╲
  if (has('NORTH', 'EAST') || has('SOUTH', 'WEST')) return 90; // NE/SW vertical
  if (has('NORTH', 'WEST') || has('SOUTH', 'EAST')) return 0; // NW/SE horizontal
  return -1;
}

// The end-tile piece that shares `sharedEdge` and renders at `angle`. Each
// edge belongs to exactly one piece per angle, so this is unique when it
// exists. Returns the piece's OTHER edge, or null when no piece matches
// (e.g. angle = -1), in which case the caller keeps the straight stub.
function capEdgeForAngle(sharedEdge: string | null, angle: number): string | null {
  if (!sharedEdge || angle < 0) return null;
  for (const [, [a, b]] of Object.entries(FORCED_SEGMENT_PORTS)) {
    if (screenAngle(a, b) !== angle) continue;
    if (a === sharedEdge) return b;
    if (b === sharedEdge) return a;
  }
  return null;
}

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
        // Default cap: mirror the next tile's piece so a zigzag reads the same
        // end-to-end. The piece that shares the exit edge and renders at the
        // next tile's screen angle is parallel to it, so it always connects and
        // never makes a square corner. With no tile beyond `next` (a 2-tile
        // drag) there is no bend to match, so keep the straight stub.
        const afterNext = i + 2 < path.length ? path[i + 2] : null;
        let capOther = entryDir;
        if (afterNext) {
          const nextAngle = screenAngle(getDirection(next, current), getDirection(next, afterNext));
          const matched = capEdgeForAngle(exitDir, nextAngle);
          if (matched) capOther = matched;
        }
        addPath(current.x, current.y, capOther, exitDir, 'both');
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
        // Default cap: mirror the previous tile's piece (see the start-tile
        // branch above for the rationale) so a zigzag reads the same end-to-end.
        const beforePrev = i - 2 >= 0 ? path[i - 2] : null;
        let capOther = exitDir;
        if (beforePrev) {
          const prevAngle = screenAngle(getDirection(prev, current), getDirection(prev, beforePrev));
          const matched = capEdgeForAngle(entryDir, prevAngle);
          if (matched) capOther = matched;
        }
        addPath(current.x, current.y, entryDir, capOther, 'both');
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
