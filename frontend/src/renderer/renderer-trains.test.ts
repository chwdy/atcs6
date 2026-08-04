import { describe, expect, it, vi } from 'vitest';

import { TRACK_EW, TRACK_NS, TRACK_NW } from '../lib/constants';
import {
  RendererTrainHandler,
  trainBodyFill,
  trimPolylineEnd,
  type TrainHandlerRenderer,
  type TrainSnap,
} from './renderer-trains';

// renderer-trains.js had no test before the migration; this is fresh coverage
// for its pure geometry helpers (the canvas-drawing entry composes these).

function makeRenderer(): TrainHandlerRenderer {
  return {
    ctx: {} as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentNetwork: {},
    currentSnapshot: {},
    debugFlags: {},
    mouseX: -1,
    mouseY: -1,
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    getNearbyTrackSegments: () => [],
    trackHandler: {
      getEdgePoints: (half: number) => ({
        N: { x: half / 2, y: -half / 2 },
        E: { x: half / 2, y: half / 2 },
        S: { x: -half / 2, y: half / 2 },
        W: { x: -half / 2, y: -half / 2 },
      }),
    },
    platformHandler: { getPlatformBodySegments: () => [] },
    labelBounds: [],
    distRectToSegments: () => 100,
    aabbOverlapArea: () => 0,
  };
}

const h = () => new RendererTrainHandler(makeRenderer());

describe('RendererTrainHandler pure helpers', () => {
  it('getOppositeEdge flips cardinal edges', () => {
    expect(h().getOppositeEdge('N')).toBe('S');
    expect(h().getOppositeEdge('E')).toBe('W');
    expect(h().getOppositeEdge('Z')).toBe('Z');
  });

  it('getActiveExitEdge follows track bits and picks the active switch branch', () => {
    // Straight track: unambiguous exit, switch state irrelevant.
    expect(h().getActiveExitEdge({ track_bits: 1 }, 'N')).toBe('S'); // NS
    expect(h().getActiveExitEdge({ track_bits: 2 }, 'E')).toBe('W'); // EW
    expect(h().getActiveExitEdge({ track_bits: 1 }, 'E')).toBeNull(); // edge carries no segment

    // EW(2)+NE(4) switch entered from E has two exits: W via the main (EW)
    // segment, N via the diverge (NE) segment. State selects which.
    const sw = { track_bits: 2 | 4, is_switch: true };
    expect(h().getActiveExitEdge({ ...sw, switch_state: 0 }, 'E')).toBe('W'); // main
    expect(h().getActiveExitEdge({ ...sw, switch_state: 1 }, 'E')).toBe('N'); // diverge

    // Diagonal-only switch NE(4)+NW(8) entered from N: state orders by bit.
    const diag = { track_bits: 4 | 8, is_switch: true };
    expect(h().getActiveExitEdge({ ...diag, switch_state: 0 }, 'N')).toBe('E'); // NE (bit 2)
    expect(h().getActiveExitEdge({ ...diag, switch_state: 1 }, 'N')).toBe('W'); // NW (bit 3)
  });

  it('getTrackExits handles straight track and switch selection', () => {
    expect(h().getTrackExits({ track_bits: 1 }, 'N')).toEqual(['S']);
    const sw = { track_bits: 1 | 4, is_switch: true, switch_state: 1 };
    expect(h().getTrackExits(sw, 'N')).toEqual(['E']);
  });

  it('getEdgeCenterOffset places the tile corner via the track handler', () => {
    expect(h().getEdgeCenterOffset('N')).toEqual({ x: 10, y: -10 });
  });

  it('getTilePathPosition interpolates entry→exit by progress', () => {
    // progress = 1 (distance == length) lands at the exit-edge ('S') offset.
    const p = h().getTilePathPosition([0, 0], 'N', 50, 50, { x: 0, y: 0, track_bits: 1 });
    expect(p.x).toBeCloseTo(-10, 6);
    expect(p.y).toBeCloseTo(10, 6);
  });

  it('getTilePathPosition slides across a configured switch instead of freezing', () => {
    // A switch with a real active length must advance with progress, not pin at
    // the entry edge. Half-way through a 200cm switch entered from W (exit E):
    // offset is the midpoint between the W and E edge centers — the tile center.
    const sw = { x: 0, y: 0, track_bits: 2 | 4, is_switch: true, switch_state: 0 };
    const mid = h().getTilePathPosition([0, 0], 'W', 100, 200, sw);
    expect(mid.x).toBeCloseTo(0, 6);
    expect(mid.y).toBeCloseTo(0, 6);
  });

  // Phase-6 contract (ported from the old tests/test_render_snapshot.py source
  // check, now that renderer-trains is TypeScript): drawTrain reads the
  // per-frame snapshot first and only falls back to window.stateManager when
  // there is no snapshot entry for the train.
  it('drawTrain renders from the per-frame snapshot when one exists (snapshot-first)', () => {
    const renderer = makeRenderer();
    const snap: TrainSnap = {
      id: 'T1',
      cab: 'A',
      facing: 'N',
      span: {
        occupiedTiles: [[0, 0]],
        leadingEnd: 'a',
        endA: { tile: [0, 0], distance_cm: 0, entry_edge: 'N' },
        endB: { tile: [0, 0], distance_cm: 0, entry_edge: 'S' },
      },
    };
    renderer.currentSnapshot = { trains: { T1: snap } };
    const handler = new RendererTrainHandler(renderer);
    const spy = vi.spyOn(handler, 'drawTrainFromSnapshot').mockImplementation(() => {});
    handler.drawTrain({ id: 'T1' });
    expect(spy).toHaveBeenCalledWith({ id: 'T1' }, snap);
  });

  it('draws a cabless unidentified train body along every tile in its bent span', () => {
    const renderer = makeRenderer();
    const lineTo = vi.fn();
    renderer.ctx = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo,
      stroke: vi.fn(),
      setLineDash: vi.fn(),
    } as unknown as CanvasRenderingContext2D;
    renderer.currentNetwork = {
      tracks: [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_NW },
        { x: 1, y: 1, track_bits: TRACK_NS },
      ],
    };
    const handler = new RendererTrainHandler(renderer);
    vi.spyOn(handler, 'drawEndMarker').mockImplementation(() => {});
    vi.spyOn(handler, 'drawTrainLabel').mockImplementation(() => {});

    handler.drawTrainFromSnapshot(
      { id: 'UNIDENT_1', is_unidentified: true },
      {
        id: 'UNIDENT_1',
        cab: null,
        facing: null,
        isUnidentified: true,
        span: {
          occupiedTiles: [[0, 0], [1, 0], [1, 1]],
          leadingEnd: '',
          endA: { tile: [0, 0], distance_cm: 25, entry_edge: 'W' },
          endB: { tile: [1, 1], distance_cm: 25, entry_edge: 'S' },
        },
      },
    );

    // The bend's shared track edge is part of both border and fill strokes.
    // A straight endpoint-to-endpoint shortcut never visits this point.
    expect(lineTo).toHaveBeenCalledWith(10, 10);
    expect(renderer.ctx.setLineDash).toHaveBeenCalledWith([4, 3]);
  });

  // The dashed body of an unidentified train reads as dashed ONLY if the whole
  // chunk is one path with butt caps. Stroking segment-by-segment restarts the
  // dash phase at every tile edge, and a round cap adds lineWidth/2 of ink past
  // both ends of every dash — wider than the 3px gap, so the dashes fuse and
  // the body renders as a solid lumpy line that no longer reads "unidentified".
  it('strokes an unidentified body as one butt-capped path per chunk, not per segment', () => {
    const renderer = makeRenderer();
    const ctx = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      setLineDash: vi.fn(),
      lineCap: 'round',
      lineJoin: 'miter',
    };
    // Record the cap in force at each stroke() — the property is mutable, so
    // only its value AT stroke time proves anything.
    const capsAtStroke: string[] = [];
    ctx.stroke = vi.fn(() => { capsAtStroke.push(ctx.lineCap); });
    renderer.ctx = ctx as unknown as CanvasRenderingContext2D;
    renderer.currentNetwork = {
      tracks: [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_NW },
        { x: 1, y: 1, track_bits: TRACK_NS },
      ],
    };
    const handler = new RendererTrainHandler(renderer);
    vi.spyOn(handler, 'drawEndMarker').mockImplementation(() => {});
    vi.spyOn(handler, 'drawTrainLabel').mockImplementation(() => {});

    handler.drawTrainFromSnapshot(
      { id: 'UNIDENT_1', is_unidentified: true },
      {
        id: 'UNIDENT_1',
        cab: null,
        facing: null,
        isUnidentified: true,
        span: {
          occupiedTiles: [[0, 0], [1, 0], [1, 1]],
          leadingEnd: '',
          endA: { tile: [0, 0], distance_cm: 25, entry_edge: 'W' },
          endB: { tile: [1, 1], distance_cm: 25, entry_edge: 'S' },
        },
      },
    );

    // One chunk, two passes (dark border then tinted fill) => exactly two
    // strokes. Per-segment stroking would emit one per polyline segment.
    expect(ctx.stroke).toHaveBeenCalledTimes(2);
    expect(ctx.beginPath).toHaveBeenCalledTimes(2);
    expect(capsAtStroke).toEqual(['butt', 'butt']);
  });

  // An unidentified body is pulled back by one end-marker at each end so its
  // inferred extent stops short of the block seam and the end squares have
  // somewhere to sit.
  describe('trimPolylineEnd', () => {
    it('shortens the leading end along the first segment', () => {
      const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
      expect(trimPolylineEnd(line, 10, true)).toEqual([{ x: 10, y: 0 }, { x: 100, y: 0 }]);
    });

    it('shortens the trailing end along the last segment', () => {
      const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
      expect(trimPolylineEnd(line, 10, false)).toEqual([{ x: 0, y: 0 }, { x: 90, y: 0 }]);
    });

    it('walks through segments the inset fully swallows', () => {
      // First segment is 4 long, so a 10 inset eats it whole and takes 6 more
      // off the second — the consumed vertex must be dropped, not left behind.
      const line = [{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 100, y: 0 }];
      expect(trimPolylineEnd(line, 10, true)).toEqual([{ x: 10, y: 0 }, { x: 100, y: 0 }]);
    });

    it('leaves a polyline shorter than the inset alone rather than folding it', () => {
      const line = [{ x: 0, y: 0 }, { x: 6, y: 0 }];
      expect(trimPolylineEnd(line, 10, true)).toEqual(line);
    });

    it('is a no-op for a degenerate polyline or a zero inset', () => {
      const single = [{ x: 1, y: 2 }];
      expect(trimPolylineEnd(single, 10, true)).toEqual(single);
      const line = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
      expect(trimPolylineEnd(line, 0, true)).toEqual(line);
    });
  });

  describe('trainBodyFill', () => {
    it('defaults to white for ATO / no special mode', () => {
      expect(trainBodyFill({})).toBe('#ffffff');
      expect(trainBodyFill({ controlMode: 'ato' })).toBe('#ffffff');
    });

    it('tints light yellow while manually driven', () => {
      expect(trainBodyFill({ controlMode: 'manual' })).toBe('#ffe066');
    });

    it('tints light purple while a route teardown is pending', () => {
      expect(trainBodyFill({ teardownPending: true })).toBe('#c8a2ff');
    });

    it('lets teardown win over manual when both apply', () => {
      expect(trainBodyFill({ controlMode: 'manual', teardownPending: true })).toBe('#c8a2ff');
    });

    it('tints saturated purple while the emergency brake is active', () => {
      expect(trainBodyFill({ ebActive: true })).toBe('#8257e5');
    });

    it('lets EB win over teardown and manual when all apply', () => {
      expect(
        trainBodyFill({ controlMode: 'manual', teardownPending: true, ebActive: true }),
      ).toBe('#8257e5');
    });

    it('tints an unidentified train steel, ahead of every mode hue', () => {
      // An unidentified train has no cab, no mode and no brake, so a mode hue would be a lie
      // about a train the app cannot drive (design_new.md "Unidentified trains").
      expect(trainBodyFill({ isUnidentified: true })).toBe('#8a8a99');
      expect(
        trainBodyFill({ isUnidentified: true, controlMode: 'manual', ebActive: true }),
      ).toBe('#8a8a99');
    });
  });
});
