// Parallel crossings vs perpendicular crossovers in the construction pathfinder.
//
// Two track segments that share no edge make a multi-path tile. They are NOT
// the same thing: NE|SW (both vertical on the 45°-rotated screen) and NW|SE
// (both horizontal) run *parallel* and never touch — a benign double-track —
// whereas NS (╱) + EW (╲) meet at the tile centre, a true crossover. The cost
// model charges nothing extra for a parallel crossing (it costs the same as a
// plain zigzag move) so the router stops detouring around an existing parallel
// track; the perpendicular crossover keeps its penalty.
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

  it('a parallel crossing costs nothing extra in normal mode, and is preferred in shift mode', () => {
    const pf = new ConstructionPathfinder();
    expect(pf.costs.createParallel).toBe(0); // == a zigzag (diagonal) move's base cost
    expect(pf.avoidConnectCosts.createParallel).toBeLessThan(pf.avoidConnectCosts.diagonal);
  });

  it('the router no longer detours around an existing parallel track', () => {
    // Existing SE tile sits on the natural (0,0)->(6,6) zigzag at (1,0), where
    // the path would lay NW — i.e. a parallel crossing. With the old crossover
    // cost the router detoured around it; now it routes straight through.
    const existing = [{ x: 1, y: 0, type: 'STRAIGHT', track_bits: 16 }]; // SE

    const pfOld = new ConstructionPathfinder();
    pfOld.costs.createParallel = pfOld.costs.createCrossover; // pre-split behavior
    const oldPath = pfOld.findPath(0, 0, 6, 6, existing, {});
    expect(oldPath?.some((p) => p.x === 1 && p.y === 0)).toBe(false); // detoured around

    const pfNew = new ConstructionPathfinder();
    const newPath = pfNew.findPath(0, 0, 6, 6, existing, {});
    expect(newPath?.some((p) => p.x === 1 && p.y === 0)).toBe(true); // routes through
  });
});
