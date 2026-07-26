import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  RendererBlockHandler,
  UNGROUPED_BLOCK_COLOR,
  type BlockHandlerRenderer,
} from './renderer-blocks';
import { blockColorCss } from '../lib/block-color';

// Coverage for the BLOCK grouping color overlay (drawBlockGroupingColors): a
// block that groups several segments paints that block's deterministic color;
// a block holding a single segment is the default "own feed" and paints faint
// grey; tiles with no block are skipped. This is the renamed power-district
// overlay — grouping is now the block itself, not a wrapper around blocks.

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
    fillStyle: '',
  };
}

function makeRenderer(
  ctx: ReturnType<typeof makeCtx>,
  drawGridSegment: ReturnType<typeof vi.fn>,
): BlockHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentBlocks: {},
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    trackHandler: {
      getEdgePoints: () => ({
        N: { x: 0, y: -1 },
        S: { x: 0, y: 1 },
        E: { x: 1, y: 0 },
        W: { x: -1, y: 0 },
      }),
      drawGridSegment,
      drawTileExtentHighlight: vi.fn(),
    },
  };
}

afterEach(() => {
  delete (globalThis as { getActiveSegments?: unknown }).getActiveSegments;
  delete (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH;
});

describe('RendererBlockHandler.drawBlockGroupingColors', () => {
  it("paints a tile of a multi-segment block with that block's color", () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawBlockGroupingColors(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      { BLK_A: { segment_ids: ['S1', 'S2'] } },
    );
    expect(drawGridSegment).toHaveBeenCalled();
    const color = drawGridSegment.mock.calls[0][5];
    expect(color).toBe(blockColorCss('BLK_A'));
  });

  it('paints a single-segment (ungrouped) block faint grey', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawBlockGroupingColors(
      [{ x: 0, y: 0, segment_id: 'BLK_Z', block_section_id: 'BLK_Z', track_bits: 1 }],
      { BLK_Z: { segment_ids: ['BLK_Z'] } },
    );
    expect(drawGridSegment).toHaveBeenCalled();
    const color = drawGridSegment.mock.calls[0][5];
    expect(color).toBe(UNGROUPED_BLOCK_COLOR);
    expect(color).toBe('hsla(0, 0%, 55%, 0.22)');
  });

  it('skips tiles that belong to no block', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawBlockGroupingColors([{ x: 0, y: 0, track_bits: 1 }], { BLK_A: { segment_ids: [] } });
    expect(drawGridSegment).not.toHaveBeenCalled();
  });

  it('tolerates a null block map (every tile reads ungrouped)', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawBlockGroupingColors(
      [{ x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 }],
      null,
    );
    expect(drawGridSegment.mock.calls[0][5]).toBe(UNGROUPED_BLOCK_COLOR);
  });

  it("falls back to the renderer's cached block map when none is passed", () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const r = makeRenderer(ctx, drawGridSegment);
    r.currentBlocks = { BLK_A: { segment_ids: ['S1', 'S2'] } };
    const h = new RendererBlockHandler(r);
    h.drawBlockGroupingColors([
      { x: 0, y: 0, segment_id: 'S1', block_section_id: 'BLK_A', track_bits: 1 },
    ]);
    expect(drawGridSegment.mock.calls[0][5]).toBe(blockColorCss('BLK_A'));
  });
});
