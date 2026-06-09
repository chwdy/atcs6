// Parallel crossings vs perpendicular crossovers in the construction pathfinder.
//
// Two track segments that share no edge make a multi-path tile. They are NOT
// the same thing: NE|SW (both vertical on the 45°-rotated screen) and NW|SE
// (both horizontal) run *parallel* and never touch — a benign double-track —
// whereas NS (╱) + EW (╲) meet at the tile centre, a true crossover. The cost
// model charges only a minimal surcharge for a parallel crossing (far below a
// switch or crossover) so the router stops detouring around an existing
// parallel track; the perpendicular crossover keeps its larger penalty.
import { describe, it, expect } from 'vitest';
import { ConstructionPathfinder } from './pathfinders';

describe('parallel crossing vs perpendicular crossover', () => {
  it('classifies the three two-segment configs distinctly', () => {
    const pf = new ConstructionPathfinder();
    expect(pf.detectSwitchType(8, 16)).toBe('parallel'); // NW over SE  (both horizontal)
    expect(pf.detectSwitchType(16, 8)).toBe('parallel'); // SE over NW
    expect(pf.detectSwitchType(4, 32)).toBe('parallel'); // NE over SW  (both vertical)
    expect(pf.detectSwitchType(32, 4)).toBe('parallel'); // SW over NE
    expect(pf.detectSwitchType(1, 2)).toBe('crossover'); // NS over EW  (perpendicular)
    expect(pf.detectSwitchType(1, 4)).toBe('switch'); // NS + NE share the N edge
    expect(pf.detectSwitchType(16, 16)).toBe('none'); // already present -> reuse, not a new segment
  });

  it('normal costs are positive integers; a parallel crossing stays near-free', () => {
    const pf = new ConstructionPathfinder();
    // Every normal cost is a positive integer (no fractions, no zeros).
    for (const v of Object.values(pf.costs)) {
      expect(Number.isInteger(v)).toBe(true);
      expect(v).toBeGreaterThan(0);
    }
    // A parallel crossing carries only a minimal surcharge — far below a switch
    // or a crossover — so the router lays it instead of detouring around.
    expect(pf.costs.createParallel).toBeLessThan(pf.costs.move);
    expect(pf.costs.createParallel).toBeLessThan(pf.costs.createCrossover);
  });

  it('shift mode penalises only moves that connect to existing track, never seeks crossings', () => {
    const pf = new ConstructionPathfinder();
    // The connecting moves cost more than in normal mode...
    expect(pf.avoidConnectCosts.useExisting).toBeGreaterThan(pf.costs.useExisting);
    expect(pf.avoidConnectCosts.extend).toBeGreaterThan(pf.costs.extend);
    expect(pf.avoidConnectCosts.createSwitch).toBeGreaterThan(pf.costs.createSwitch);
    // ...while the independent overlaps are never made negative (no "seek a
    // crossover" incentive). A perpendicular crossover keeps its normal cost; a
    // parallel double-track is free — it costs the same as the plain move under
    // it (surcharge 0), since it never joins the existing track.
    expect(pf.avoidConnectCosts.createCrossover).toBe(pf.costs.createCrossover);
    expect(pf.avoidConnectCosts.createParallel).toBe(0);
    expect(pf.avoidConnectCosts.createParallel).toBeGreaterThanOrEqual(0);
    expect(pf.avoidConnectCosts.createCrossover).toBeGreaterThanOrEqual(0);
    // Merging is penalised harder than crossing, so a forced pass-through lays
    // a crossing rather than a switch.
    expect(pf.avoidConnectCosts.createSwitch).toBeGreaterThan(pf.avoidConnectCosts.createCrossover);
    // Both modes share the same move/curve model: a straight step and a diagonal
    // step cost the same base, and only a heading change is taxed (curve). So a
    // clean straight run and a clean 45° diagonal are equally cheap in each mode.
    expect(pf.avoidConnectCosts.move).toBe(pf.costs.move);
    expect(pf.avoidConnectCosts.curve).toBe(pf.costs.curve);
  });

  it('never lengthens the path to avoid a parallel crossing', () => {
    // Existing SE tile sits on the natural (0,0)->(6,6) zigzag at (1,0), where
    // the path would otherwise lay NW — a parallel crossing. The minimal
    // surcharge is far below one tile's move cost, so the router never adds
    // tiles to route around it: the path is exactly as long as on empty grid.
    const existing = [{ x: 1, y: 0, type: 'STRAIGHT', track_bits: 16 }]; // SE
    const baseline = new ConstructionPathfinder().findPath(0, 0, 6, 6, [], {})!;
    const withParallel = new ConstructionPathfinder().findPath(0, 0, 6, 6, existing, {})!;
    expect(withParallel.length).toBe(baseline.length); // no detour
    // The surcharge does, however, break the tie in favour of keeping the two
    // tracks side by side rather than stacked, when that costs no extra length.
    expect(withParallel.some((p) => p.x === 1 && p.y === 0)).toBe(false);
  });
});
