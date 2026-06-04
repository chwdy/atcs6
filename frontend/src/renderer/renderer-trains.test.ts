import { describe, expect, it, vi } from 'vitest';

import { RendererTrainHandler, trainBodyFill, type TrainHandlerRenderer, type TrainSnap } from './renderer-trains';

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

  it('getExitEdge follows the track bits', () => {
    expect(h().getExitEdge(1, 'N')).toBe('S'); // NS
    expect(h().getExitEdge(2, 'E')).toBe('W'); // EW
    expect(h().getExitEdge(1, 'E')).toBeNull();
  });

  it('getTrackExits handles straight track and switch selection', () => {
    expect(h().getTrackExits({ track_bits: 1 }, 'N')).toEqual(['S']);
    const sw = { track_bits: 1 | 4, is_switch: true, switch_state: 1 };
    expect(h().getTrackExits(sw, 'N')).toEqual(['E']);
  });

  it('getEdgeCenterOffset and getEdgePoints place the tile corners', () => {
    expect(h().getEdgeCenterOffset('N')).toEqual({ x: 10, y: -10 });
    expect(h().getEdgePoints(40).S).toEqual({ x: -20, y: 20 });
  });

  it('getTilePathPosition interpolates entry→exit by progress', () => {
    // progress = 1 (distance == length) lands at the exit-edge ('S') offset.
    const p = h().getTilePathPosition([0, 0], 'N', 50, 50, 1);
    expect(p.x).toBeCloseTo(-10, 6);
    expect(p.y).toBeCloseTo(10, 6);
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
  });
});
