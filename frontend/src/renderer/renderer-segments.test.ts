import { afterEach, describe, expect, it, vi } from 'vitest';

import { RendererSegmentHandler, type SegmentHandlerRenderer } from './renderer-segments';

// Coverage for the SEGMENT (geometry) overlay: per-segment colors, the preview
// path, and the metrics box. Geometry (length / radius / speed) is read off the
// segment; voltage is read off the segment's owning block, because power is a
// block property that every member segment shares.

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
    strokeRect: vi.fn(),
    setLineDash: vi.fn(),
    roundRect: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn(() => ({ width: 20 })),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    lineCap: '',
    font: '',
    textAlign: '',
    textBaseline: '',
  };
}

interface RendererOpts {
  drawGridSegment?: ReturnType<typeof vi.fn>;
  drawTileExtentHighlight?: ReturnType<typeof vi.fn>;
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>, opts: RendererOpts = {}): SegmentHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentSegments: {},
    currentBlocks: {},
    segmentColorCache: {},
    segmentColorCounter: 0,
    mouseX: -1,
    mouseY: -1,
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    trackHandler: {
      getEdgePoints: () => ({
        N: { x: 0, y: -1 },
        S: { x: 0, y: 1 },
        E: { x: 1, y: 0 },
        W: { x: -1, y: 0 },
      }),
      drawGridSegment: opts.drawGridSegment ?? vi.fn(),
      drawTileExtentHighlight: opts.drawTileExtentHighlight ?? vi.fn(),
    },
  };
}

afterEach(() => {
  delete (globalThis as { getActiveSegments?: unknown }).getActiveSegments;
  delete (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH;
});

describe('RendererSegmentHandler.getSegmentColor', () => {
  it('assigns a stable hsla color per segment id and caches it', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    const h = new RendererSegmentHandler(r);
    const c1 = h.getSegmentColor('S1');
    expect(c1).toMatch(/^hsla\(/);
    expect(h.getSegmentColor('S1')).toBe(c1); // cached → identical
    expect(h.getSegmentColor(null)).toBeNull();
    expect(r.segmentColorCounter).toBe(1); // only one distinct segment colored
  });
});

describe('RendererSegmentHandler.drawSegmentIdColors', () => {
  it('draws one grid segment per active edge pair of a tile in a segment', () => {
    (globalThis as { getActiveSegments?: unknown }).getActiveSegments = () => [['N', 'S']];
    (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH = 12;
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererSegmentHandler(makeRenderer(ctx, { drawGridSegment }));
    h.drawSegmentIdColors([{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'S1', track_bits: 1 }]);
    expect(drawGridSegment).toHaveBeenCalledTimes(1);
  });

  it('skips tiles that belong to no segment', () => {
    (globalThis as { getActiveSegments?: unknown }).getActiveSegments = () => [['N', 'S']];
    (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH = 12;
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererSegmentHandler(makeRenderer(ctx, { drawGridSegment }));
    h.drawSegmentIdColors([{ x: 0, y: 0, track_bits: 1 }]);
    expect(drawGridSegment).not.toHaveBeenCalled();
  });

  it('colors by segment, not by owning block — two segments of one block differ', () => {
    (globalThis as { getActiveSegments?: unknown }).getActiveSegments = () => [['N', 'S']];
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererSegmentHandler(makeRenderer(ctx, { drawGridSegment }));
    h.drawSegmentIdColors([
      { x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 },
      { x: 1, y: 0, segment_id: 'S2', block_section_id: 'BLK_A', track_bits: 1 },
    ]);
    const first = drawGridSegment.mock.calls[0][5];
    const second = drawGridSegment.mock.calls[1][5];
    expect(first).not.toBe(second);
  });
});

describe('RendererSegmentHandler.drawSegmentPreviewPath', () => {
  it('delegates to the shared track-extent overlay with the path tiles as keys', () => {
    const ctx = makeCtx();
    const drawTileExtentHighlight = vi.fn();
    const h = new RendererSegmentHandler(makeRenderer(ctx, { drawTileExtentHighlight }));
    h.drawSegmentPreviewPath([{ x: 0, y: 0 }, { x: 1, y: 0 }]);
    expect(drawTileExtentHighlight).toHaveBeenCalledTimes(1);
    const keys = drawTileExtentHighlight.mock.calls[0][0] as Set<string>;
    expect([...keys].sort()).toEqual(['0,0', '1,0']);
  });

  it('draws nothing for an empty path', () => {
    const ctx = makeCtx();
    const drawTileExtentHighlight = vi.fn();
    const h = new RendererSegmentHandler(makeRenderer(ctx, { drawTileExtentHighlight }));
    h.drawSegmentPreviewPath([]);
    expect(drawTileExtentHighlight).not.toHaveBeenCalled();
  });
});

describe('RendererSegmentHandler.drawSegmentMetrics', () => {
  it('puts a forward (positive) block voltage on the + rail and pills the segment', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { BLK_A: { current_voltage: 6 } };
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { voltage: true },
    );

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+6.0');
    expect(texts).toContain('−0.0');
    expect(ctx.roundRect).toHaveBeenCalledTimes(1); // energized → pill drawn
  });

  it('puts a reverse (negative) block voltage on the − rail', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { BLK_A: { current_voltage: -6 } };
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { voltage: true },
    );

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+0.0');
    expect(texts).toContain('−6.0');
  });

  it('labels both rails but draws no pill for an unpowered block (0V)', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { BLK_A: { current_voltage: 0 } };
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { voltage: true },
    );

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+0.0');
    expect(texts).toContain('−0.0');
    expect(ctx.roundRect).not.toHaveBeenCalled(); // unpowered → no pill
  });

  it('every segment of one grouped block reads that block\'s shared voltage', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { BLK_A: { current_voltage: 6 } };
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [
        { x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 },
        { x: 1, y: 0, segment_id: 'S2', block_section_id: 'BLK_A', track_bits: 1 },
      ],
      { voltage: true },
    );

    const plusRails = ctx.fillText.mock.calls.map((c) => c[0]).filter((t) => t === '+6.0');
    expect(plusRails).toHaveLength(2); // one per member segment
  });

  it('renders length, radius and speed from the SEGMENT with placeholders for missing values', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentSegments = { S1: { length_cm: 60, speed_limit_kmh: 45 } }; // no radius set
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { length: true, radius: true, speed: true },
    );

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('60cm');
    expect(texts).toContain('R--'); // radius absent → dim placeholder
    expect(texts).toContain('45km/h');
  });

  it('draws a single consolidated box when several metrics are enabled', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentSegments = { S1: { length_cm: 60, speed_limit_kmh: 45 } };
    r.currentBlocks = { BLK_A: { current_voltage: 6 } };
    const h = new RendererSegmentHandler(r);
    h.drawSegmentMetrics(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { voltage: true, length: true, radius: true, speed: true },
    );

    // One box for all four rows, not one pill per metric.
    expect(ctx.roundRect).toHaveBeenCalledTimes(1);
  });

  it('skips tiles that belong to no segment', () => {
    const ctx = makeCtx();
    const h = new RendererSegmentHandler(makeRenderer(ctx));
    h.drawSegmentMetrics([{ x: 0, y: 0, track_bits: 1 }], { voltage: true });
    expect(ctx.fillText).not.toHaveBeenCalled();
  });
});
