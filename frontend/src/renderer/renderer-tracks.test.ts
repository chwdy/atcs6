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

import { RendererTrackHandler, blockTrackColor, type TrackHandlerRenderer, type Track } from './renderer-tracks';

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
    clip: vi.fn(),
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
    trackMap: {},
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    signalHandler: { drawSingleSignal: vi.fn() },
    debugHandler: { highlightDeadEnds: vi.fn() },
  };
}

const EXTENT_STYLE = {
  tileFill: 'fill', glow: 'glow', core: 'core', cap: 'cap',
};

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

  it('drawTrackFromBits fills an extended band per segment, plus a crossing dot fill when applicable', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    mocks.isCrossing.mockReturnValue(false);
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTrackFromBits(1, 40, '#666', []);
    expect(ctx.fill).toHaveBeenCalledTimes(1); // one filled colored band (no stroke)
    expect(ctx.stroke).not.toHaveBeenCalled();

    mocks.isCrossing.mockReturnValue(true);
    const ctx2 = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx2)).drawTrackFromBits(3, 40, '#666', []);
    expect(ctx2.fill).toHaveBeenCalledTimes(2); // one band fill (mock returns 1 segment) + crossing dot
    expect(ctx2.stroke).not.toHaveBeenCalled();
  });

  it('drawCenterlineBand fills one rectangle, offset perpendicular to the path', () => {
    const ctx = makeCtx();
    // Horizontal path (0,0)->(10,0): the true normal is vertical, so the first
    // corner is the start point offset by +halfWidth in y (NOT a diagonal offset).
    new RendererTrackHandler(makeRenderer(ctx)).drawCenterlineBand({ x: 0, y: 0 }, { x: 10, y: 0 }, 4, '#666');
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.lineTo).toHaveBeenCalledTimes(3);
    expect(ctx.moveTo).toHaveBeenCalledWith(0, 2);
  });

  it('drawTrackBed fills a grey trapezoid band per segment (empty tile draws nothing)', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTrackBed(1, 40);
    expect(ctx.fill).toHaveBeenCalledTimes(1); // one grey trapezoid (drawCenterlineBand)

    mocks.getActiveSegments.mockReturnValue([]);
    const ctxEmpty = makeCtx();
    new RendererTrackHandler(makeRenderer(ctxEmpty)).drawTrackBed(0, 40);
    expect(ctxEmpty.fill).not.toHaveBeenCalled();
  });

  it('drawTrackWithSwitches lays the selected arm full + coloured and cuts the unselected arm to a short grey stub', () => {
    // Regular turnout EW(2) | NE(4): at switch_state 0 the long EW arm is the
    // selected (MAIN) route and the NE diagonal is unselected. The unselected arm
    // is CUT to a short bed stub — not painted over / erased, not dim-tinted — so
    // it reads as not connected at the switch with no stray triangles.
    const fills: string[] = [];
    const ctx = makeCtx();
    let fillStyle = '';
    Object.defineProperty(ctx, 'fillStyle', { get: () => fillStyle, set: (v) => { fillStyle = v; } });
    ctx.fill = vi.fn(() => { fills.push(fillStyle); });
    new RendererTrackHandler(makeRenderer(ctx))
      .drawTrackWithSwitches(6, 40, '#abcdef', 0, false, { E: ['N', 'W'] });
    expect(fills).toContain('#abcdef'); // selected EW arm in the route colour
    // Two grey beds: the selected arm's full bed + the unselected arm's stub.
    expect(fills.filter((f) => f === '#666666')).toHaveLength(2);
    expect(fills).not.toContain('#0a0a0c'); // not erased to the background
  });
});

describe('RendererTrackHandler.drawTileExtentHighlight', () => {
  it('lights the track stroke and caps the run ends (no cell fill for track tiles)', () => {
    // Two stacked NS tiles. getActiveSegments is mocked to [['N','S']];
    // trackBitsToEdges(1) is real -> {N, S}. (0,0)'s N neighbor is (0,1) and
    // (0,1)'s S neighbor is (0,0) — both in the set — so only the two outer
    // ends of the run get a cap.
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.trackMap = { '0,0': { track_bits: 1 }, '0,1': { track_bits: 1 } };
    new RendererTrackHandler(r).drawTileExtentHighlight(new Set(['0,0', '0,1']), EXTENT_STYLE);
    expect(ctx.fill).not.toHaveBeenCalled(); // track tiles show as rail, not a diamond
    // 2 glow + 2 core segment strokes, then 2 end-cap ticks (run ends only).
    expect(ctx.stroke).toHaveBeenCalledTimes(6);
  });

  it('draws a dim diamond (and no stroke) only for a selected tile with no track', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx); // trackMap is empty -> 3,3 carries no track
    new RendererTrackHandler(r).drawTileExtentHighlight(new Set(['3,3']), EXTENT_STYLE);
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.stroke).not.toHaveBeenCalled();
  });

  it('draws nothing for an empty set', () => {
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTileExtentHighlight(new Set(), EXTENT_STYLE);
    expect(ctx.fill).not.toHaveBeenCalled();
    expect(ctx.stroke).not.toHaveBeenCalled();
  });
});
