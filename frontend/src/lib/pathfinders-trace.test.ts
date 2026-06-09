import { describe, it, expect } from 'vitest';
import { ConstructionPathfinder, traceNodeKey, type TraceNode } from './pathfinders';
import { graphSearch } from './graph-search';

// Exercises followExistingNeighbors + graphSearch the same way the editor's
// _traceTrackPath / _traceReachableTiles now do, on small hand-built networks.
const pf = new ConstructionPathfinder();
const mapOf = (tracks: any[]) => new Map<string, any>(tracks.map((t) => [`${t.x},${t.y}`, t]));

const tracePath = (tracks: any[], start: any, goal: any, opts = {}) => {
  const byKey = mapOf(tracks);
  if (!byKey.has(`${start.x},${start.y}`) || !byKey.has(`${goal.x},${goal.y}`)) return null;
  const { path } = graphSearch<TraceNode>({
    seeds: [{ x: start.x, y: start.y, entry: '' }],
    key: traceNodeKey,
    neighbors: pf.followExistingNeighbors(byKey, opts),
    goal: (n) => n.x === goal.x && n.y === goal.y,
  });
  return path ? path.map((n) => ({ x: n.x, y: n.y })) : null;
};

const reachable = (tracks: any[], start: any, opts = {}) => {
  const byKey = mapOf(tracks);
  const out = new Set<string>();
  const { visited } = graphSearch<TraceNode>({
    seeds: [{ x: start.x, y: start.y, entry: '' }],
    key: traceNodeKey,
    neighbors: pf.followExistingNeighbors(byKey, opts),
  });
  for (const n of visited.values()) {
    const k = `${n.x},${n.y}`;
    if (k !== `${start.x},${start.y}`) out.add(k);
  }
  return out;
};

// bits: NS=1 EW=2 NE=4 NW=8 SE=16 SW=32
describe('followExistingNeighbors via graphSearch (trace)', () => {
  it('follows a straight EW line and reports reachable tiles', () => {
    const net = [
      { x: 0, y: 0, track_bits: 2 },
      { x: 1, y: 0, track_bits: 2 },
      { x: 2, y: 0, track_bits: 2 },
    ];
    expect(tracePath(net, { x: 0, y: 0 }, { x: 2, y: 0 })).toEqual([
      { x: 0, y: 0 },
      { x: 1, y: 0 },
      { x: 2, y: 0 },
    ]);
    expect([...reachable(net, { x: 0, y: 0 })].sort()).toEqual(['1,0', '2,0']);
  });

  it('start === goal returns the single tile', () => {
    const net = [{ x: 0, y: 0, track_bits: 2 }];
    expect(tracePath(net, { x: 0, y: 0 }, { x: 0, y: 0 })).toEqual([{ x: 0, y: 0 }]);
  });

  it('does not cheat across the disconnected segments of a switch', () => {
    // (0,1) is a switch carrying NS (1) + SE (16). Entering from NORTH binds to
    // the NS segment → can only exit SOUTH (not EAST onto the SE curve).
    const net = [
      { x: 0, y: 0, track_bits: 1 }, // NS below the switch
      { x: 0, y: 1, track_bits: 1 | 16 }, // switch: NS + SE
      { x: 0, y: 2, track_bits: 1 }, // NS above the switch
      { x: 1, y: 1, track_bits: 2 }, // EW to the east (the SE branch)
    ];
    // From the north (enters switch via NS): reaches the south leg, NOT the east branch.
    const fromNorth = reachable(net, { x: 0, y: 2 });
    expect(fromNorth.has('0,0')).toBe(true);
    expect(fromNorth.has('1,1')).toBe(false);
    // From the east branch (enters switch via SE): reaches the south leg, NOT the north leg.
    const fromEast = reachable(net, { x: 1, y: 1 });
    expect(fromEast.has('0,0')).toBe(true);
    expect(fromEast.has('0,2')).toBe(false);
  });

  it('stopAtSwitches refuses to cross a switch tile (block/platform rule)', () => {
    const net = [
      { x: 0, y: 0, track_bits: 2 },
      { x: 1, y: 0, track_bits: 2, is_switch: true }, // switch in the middle
      { x: 2, y: 0, track_bits: 2 },
    ];
    // Plain trace crosses it; a block walk (stopAtSwitches) cannot.
    expect(tracePath(net, { x: 0, y: 0 }, { x: 2, y: 0 })).not.toBeNull();
    expect(tracePath(net, { x: 0, y: 0 }, { x: 2, y: 0 }, { stopAtSwitches: true })).toBeNull();
  });
});
