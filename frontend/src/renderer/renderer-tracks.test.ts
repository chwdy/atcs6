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

    it('maps live state to the canvas palette', () => {
      expect(blockTrackColor({ state: 'occupied' })).toBe('#e83f5b');
      expect(blockTrackColor({ state: 'reserved' })).toBe('#04d361');
      expect(blockTrackColor({ state: 'free' })).toBe('#4285f4');
      expect(blockTrackColor({ state: 'caution' })).toBe('#ffcd1e');
      expect(blockTrackColor({})).toBe('#4285f4'); // missing state -> free
      expect(blockTrackColor({ state: 'mystery' })).toBeNull(); // unknown -> default
    });

    it('renders disabled blocks grey and manual blocks orange', () => {
      expect(blockTrackColor({ disabled: true })).toBe('#6c6c78');
      expect(blockTrackColor({ manual: true })).toBe('#ff9500');
    });

    it('lets availability override live state, with disabled winning over manual', () => {
      // A manual or disabled block reads its availability color even when occupied.
      expect(blockTrackColor({ manual: true, state: 'occupied' })).toBe('#ff9500');
      expect(blockTrackColor({ disabled: true, state: 'occupied' })).toBe('#6c6c78');
      expect(blockTrackColor({ disabled: true, manual: true, state: 'occupied' })).toBe('#6c6c78');
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
