import { afterEach, describe, expect, it, vi } from 'vitest';

// renderer-tracks imports getActiveSegments/isCrossing from ../lib/constants;
// mock just those two so drawTrackFromBits's fill-counting is tested in
// isolation from the real bit math (the rest of constants stays real).
const mocks = vi.hoisted(() => ({
  getActiveSegments: vi.fn((): Array<[string, string]> => [['N', 'S']]),
  isCrossing: vi.fn((): boolean => false),
}));
vi.mock('../lib/constants', async (importActual) => {
  const actual = await importActual<typeof import('../lib/constants')>();
  return { ...actual, getActiveSegments: mocks.getActiveSegments, isCrossing: mocks.isCrossing };
});

import { RendererTrackHandler, type TrackHandlerRenderer } from './renderer-tracks';

// renderer-tracks.js had no test before the migration; this is fresh coverage
// for the shared geometry (getEdgePoints/drawGridSegment) and bit-driven draw.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    translate: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fillText: vi.fn(),
    setLineDash: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    lineCap: '',
    font: '',
    textAlign: '',
    textBaseline: '',
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): TrackHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentBlocks: {},
    currentNetwork: {},
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    signalHandler: { drawSingleSignal: vi.fn() },
    debugHandler: { highlightDeadEnds: vi.fn() },
  };
}

afterEach(() => {
  mocks.getActiveSegments.mockReset();
  mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
  mocks.isCrossing.mockReset();
  mocks.isCrossing.mockReturnValue(false);
});

describe('RendererTrackHandler', () => {
  it('getEdgePoints returns the four tile-corner points', () => {
    const ep = new RendererTrackHandler(makeRenderer(makeCtx())).getEdgePoints(40);
    expect(ep.N).toEqual({ x: 20, y: -20 });
    expect(ep.E).toEqual({ x: 20, y: 20 });
    expect(ep.S).toEqual({ x: -20, y: 20 });
    expect(ep.W).toEqual({ x: -20, y: -20 });
  });

  it('drawGridSegment fills one quadrilateral', () => {
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawGridSegment(
      { x: 0, y: 0 }, { x: 10, y: 0 }, 4, 'N', 'S', '#666',
    );
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.lineTo).toHaveBeenCalledTimes(3);
  });

  it('drawTrackFromBits fills each segment, plus a crossing dot when applicable', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    mocks.isCrossing.mockReturnValue(false);
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTrackFromBits(1, 40, '#666', []);
    expect(ctx.fill).toHaveBeenCalledTimes(1);

    mocks.isCrossing.mockReturnValue(true);
    const ctx2 = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx2)).drawTrackFromBits(3, 40, '#666', []);
    expect(ctx2.fill).toHaveBeenCalledTimes(2); // segment quad + crossing dot
  });
});
