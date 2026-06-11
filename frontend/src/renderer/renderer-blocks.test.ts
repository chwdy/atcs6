import { afterEach, describe, expect, it, vi } from 'vitest';

import { RendererBlockHandler, type BlockHandlerRenderer } from './renderer-blocks';

// renderer-blocks.js had no test before the migration; this is fresh coverage.

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

function makeRenderer(ctx: ReturnType<typeof makeCtx>, opts: RendererOpts = {}): BlockHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentBlocks: {},
    blockColorCache: {},
    blockColorCounter: 0,
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

describe('RendererBlockHandler.getBlockColor', () => {
  it('assigns a stable hsla color per block id and caches it', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    const h = new RendererBlockHandler(r);
    const c1 = h.getBlockColor('B1');
    expect(c1).toMatch(/^hsla\(/);
    expect(h.getBlockColor('B1')).toBe(c1); // cached → identical
    expect(h.getBlockColor(null)).toBeNull();
    expect(r.blockColorCounter).toBe(1); // only one distinct block colored
  });
});

describe('RendererBlockHandler.drawBlockIdColors', () => {
  it('draws one grid segment per active edge pair of a blocked tile', () => {
    (globalThis as { getActiveSegments?: unknown }).getActiveSegments = () => [['N', 'S']];
    (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH = 12;
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, { drawGridSegment }));
    h.drawBlockIdColors([{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }]);
    expect(drawGridSegment).toHaveBeenCalledTimes(1);
  });

  it('skips tiles with no block section', () => {
    (globalThis as { getActiveSegments?: unknown }).getActiveSegments = () => [['N', 'S']];
    (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH = 12;
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, { drawGridSegment }));
    h.drawBlockIdColors([{ x: 0, y: 0, track_bits: 1 }]);
    expect(drawGridSegment).not.toHaveBeenCalled();
  });
});

describe('RendererBlockHandler.drawBlockPreviewPath', () => {
  it('delegates to the shared track-extent overlay with the path tiles as keys', () => {
    const ctx = makeCtx();
    const drawTileExtentHighlight = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, { drawTileExtentHighlight }));
    h.drawBlockPreviewPath([{ x: 0, y: 0 }, { x: 1, y: 0 }]);
    expect(drawTileExtentHighlight).toHaveBeenCalledTimes(1);
    const keys = drawTileExtentHighlight.mock.calls[0][0] as Set<string>;
    expect([...keys].sort()).toEqual(['0,0', '1,0']);
  });

  it('draws nothing for an empty path', () => {
    const ctx = makeCtx();
    const drawTileExtentHighlight = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, { drawTileExtentHighlight }));
    h.drawBlockPreviewPath([]);
    expect(drawTileExtentHighlight).not.toHaveBeenCalled();
  });
});

describe('RendererBlockHandler.drawBlockMetrics', () => {
  it('puts a forward (positive) voltage on the + rail and pills the block', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { B1: { current_voltage: 6 } };
    const h = new RendererBlockHandler(r);
    h.drawBlockMetrics([{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }], { voltage: true });

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+6.0');
    expect(texts).toContain('−0.0');
    expect(ctx.roundRect).toHaveBeenCalledTimes(1); // energized → pill drawn
  });

  it('puts a reverse (negative) voltage on the − rail', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { B1: { current_voltage: -6 } };
    const h = new RendererBlockHandler(r);
    h.drawBlockMetrics([{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }], { voltage: true });

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+0.0');
    expect(texts).toContain('−6.0');
  });

  it('labels both rails but draws no pill for an unpowered block (0V)', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { B1: { current_voltage: 0 } };
    const h = new RendererBlockHandler(r);
    h.drawBlockMetrics([{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }], { voltage: true });

    const texts = ctx.fillText.mock.calls.map((c) => c[0]);
    expect(texts).toContain('+0.0');
    expect(texts).toContain('−0.0');
    expect(ctx.roundRect).not.toHaveBeenCalled(); // unpowered → no pill
  });

  it('renders length, radius and speed rows with placeholders for missing values', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentBlocks = { B1: { length_cm: 60, speed_limit_kmh: 45 } }; // no radius set
    const h = new RendererBlockHandler(r);
    h.drawBlockMetrics(
      [{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }],
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
    r.currentBlocks = { B1: { current_voltage: 6, length_cm: 60, speed_limit_kmh: 45 } };
    const h = new RendererBlockHandler(r);
    h.drawBlockMetrics(
      [{ x: 0, y: 0, block_section_id: 'B1', track_bits: 1 }],
      { voltage: true, length: true, radius: true, speed: true },
    );

    // One box for all four rows, not one pill per metric.
    expect(ctx.roundRect).toHaveBeenCalledTimes(1);
  });

  it('skips tiles with no block section', () => {
    const ctx = makeCtx();
    const h = new RendererBlockHandler(makeRenderer(ctx));
    h.drawBlockMetrics([{ x: 0, y: 0, track_bits: 1 }], { voltage: true });
    expect(ctx.fillText).not.toHaveBeenCalled();
  });
});
