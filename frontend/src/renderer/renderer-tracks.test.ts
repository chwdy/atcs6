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

// blockTrackColor now delegates to the --block-* token consumer; mock it so the
// unit test verifies the delegation without depending on getComputedStyle.
vi.mock('../lib/helpers', () => ({
  getBlockStateColor: (state: string) => `token:${state}`,
}));

import { RendererTrackHandler, blockTrackColor, type TrackHandlerRenderer } from './renderer-tracks';

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

  describe('blockTrackColor', () => {
    it('returns null for a missing block (caller keeps its default)', () => {
      expect(blockTrackColor(undefined)).toBeNull();
      expect(blockTrackColor(null)).toBeNull();
    });

    it('delegates each live state to the --block-* token consumer', () => {
      expect(blockTrackColor({ state: 'occupied' })).toBe('token:occupied');
      expect(blockTrackColor({ state: 'reserved' })).toBe('token:reserved');
      expect(blockTrackColor({ state: 'free' })).toBe('token:free');
      expect(blockTrackColor({ state: 'caution' })).toBe('token:caution');
      expect(blockTrackColor({})).toBe('token:free'); // missing state -> free
      expect(blockTrackColor({ state: 'mystery' })).toBeNull(); // unknown -> default
    });

    it('maps availability (disabled/manual) to its --block-* token', () => {
      expect(blockTrackColor({ disabled: true })).toBe('token:disabled');
      expect(blockTrackColor({ manual: true })).toBe('token:manual');
    });

    it('lets availability override live state, with disabled winning over manual', () => {
      // A manual or disabled block reads its availability token even when occupied.
      expect(blockTrackColor({ manual: true, state: 'occupied' })).toBe('token:manual');
      expect(blockTrackColor({ disabled: true, state: 'occupied' })).toBe('token:disabled');
      expect(blockTrackColor({ disabled: true, manual: true, state: 'occupied' })).toBe('token:disabled');
    });
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
