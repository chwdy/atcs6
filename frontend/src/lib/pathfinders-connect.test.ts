import { describe, it, expect } from 'vitest';
import { pathToTracks } from './pathfinders';

// When a draw STARTS or ENDS on a tile that already has track, the last piece
// must reuse the existing rail's segment when it shares the edge the draw meets
// — not lay a crossing stub that merges into a phantom switch. The start tile
// always did this; these tests pin the end tile to the same behavior, and pin
// the cases that must stay crossovers.
//
// Networks use the REAL serialized track shape (track_bits + edges only; the
// live payload carries no directed_paths/paths/connections), so the
// neighbor-snap stays inert and we exercise the same path the running app does.
//
// bits: NS=1 EW=2 NE=4 NW=8 SE=16 SW=32
const NE = 4, NW = 8, SE = 16, SW = 32, NS = 1, EW = 2;

// new track_bits emitted by a drag along `path` over the existing `tracks`.
const emitAt = (path: { x: number; y: number }[], tracks: any[], x: number, y: number) => {
  const out = pathToTracks(path, { tracks });
  const t = out.find((tr) => tr.x === x && tr.y === y);
  return t ? (t.track_bits ?? 0) : -1; // -1 = tile not emitted
};
const tile = (x: number, y: number, bits: number) => ({ x, y, track_bits: bits, type: 'STRAIGHT' });

describe('drawing onto an existing-track tile reuses the rail (end-tile fix)', () => {
  // Drag that ENDS on an isolated diagonal, arriving along one of the diagonal's
  // two edges, must reuse that diagonal — never add a perpendicular bit.
  const diagCases: [string, number, { x: number; y: number }[]][] = [
    // [name, existing diagonal bits, path arriving along a matching edge]
    ['NE from north', NE, [{ x: 0, y: 2 }, { x: 0, y: 1 }, { x: 0, y: 0 }]],
    ['NE from east', NE, [{ x: 2, y: 0 }, { x: 1, y: 0 }, { x: 0, y: 0 }]],
    ['NW from north', NW, [{ x: 0, y: 2 }, { x: 0, y: 1 }, { x: 0, y: 0 }]],
    ['NW from west', NW, [{ x: -2, y: 0 }, { x: -1, y: 0 }, { x: 0, y: 0 }]],
    ['SE from south', SE, [{ x: 0, y: -2 }, { x: 0, y: -1 }, { x: 0, y: 0 }]],
    ['SE from east', SE, [{ x: 2, y: 0 }, { x: 1, y: 0 }, { x: 0, y: 0 }]],
    ['SW from south', SW, [{ x: 0, y: -2 }, { x: 0, y: -1 }, { x: 0, y: 0 }]],
    ['SW from west', SW, [{ x: -2, y: 0 }, { x: -1, y: 0 }, { x: 0, y: 0 }]],
  ];
  for (const [name, bits, path] of diagCases) {
    it(`end on ${name} -> reuses the diagonal, no phantom switch`, () => {
      // Before the fix this emitted a straight (NS/EW) and merged to a switch.
      expect(emitAt(path, [tile(0, 0, bits)], 0, 0)).toBe(bits);
    });
  }

  it('start on a diagonal stays clean (unchanged baseline)', () => {
    // Drag leaving an NE tile to the north reuses NE (the start tile always did).
    expect(emitAt([{ x: 0, y: 0 }, { x: 0, y: 1 }, { x: 0, y: 2 }], [tile(0, 0, NE)], 0, 0)).toBe(NE);
  });
});

describe('perpendicular and colinear meetings are unchanged', () => {
  const ewLine = [tile(-1, 0, EW), tile(0, 0, EW), tile(1, 0, EW)];

  it('vertical drag into an EW line stays a crossover (both ends)', () => {
    const endNew = emitAt([{ x: 0, y: 3 }, { x: 0, y: 2 }, { x: 0, y: 1 }, { x: 0, y: 0 }], ewLine, 0, 0);
    const startNew = emitAt([{ x: 0, y: 0 }, { x: 0, y: 1 }, { x: 0, y: 2 }, { x: 0, y: 3 }], ewLine, 0, 0);
    expect(endNew).toBe(NS); // merges to NS|EW = crossover
    expect(startNew).toBe(NS);
  });

  it('colinear drag into an EW line stays EW (no spurious bit)', () => {
    expect(emitAt([{ x: -3, y: 0 }, { x: -2, y: 0 }, { x: -1, y: 0 }], ewLine, -1, 0)).toBe(EW);
  });
});
