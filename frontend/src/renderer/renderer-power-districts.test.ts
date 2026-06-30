import { afterEach, describe, expect, it, vi } from 'vitest';

import { RendererBlockHandler, type BlockHandlerRenderer } from './renderer-blocks';
import { districtColorCss } from '../lib/district-color';

// Coverage for the power-district color overlay (drawPowerDistrictColors): every
// member of a district paints that district's deterministic color; an ungrouped
// block paints a faint grey; tiles with no block are skipped.

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

function makeRenderer(ctx: ReturnType<typeof makeCtx>, drawGridSegment: ReturnType<typeof vi.fn>): BlockHandlerRenderer {
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
      drawGridSegment,
      drawTileExtentHighlight: vi.fn(),
    },
  };
}

afterEach(() => {
  delete (globalThis as { getActiveSegments?: unknown }).getActiveSegments;
  delete (globalThis as { DEBUG_BLOCK_WIDTH?: unknown }).DEBUG_BLOCK_WIDTH;
});

describe('RendererBlockHandler.drawPowerDistrictColors', () => {
  it('paints a member block with its district color', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawPowerDistrictColors(
      [{ x: 0, y: 0, block_section_id: 'BLK_A', track_bits: 1 }],
      [{ district_id: 'D1', block_ids: ['BLK_A'] }],
    );
    expect(drawGridSegment).toHaveBeenCalled();
    const color = drawGridSegment.mock.calls[0][5];
    expect(color).toBe(districtColorCss('D1'));
  });

  it('paints an ungrouped block faint grey', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawPowerDistrictColors([{ x: 0, y: 0, block_section_id: 'BLK_Z', track_bits: 1 }], []);
    expect(drawGridSegment).toHaveBeenCalled();
    const color = drawGridSegment.mock.calls[0][5];
    expect(color).toBe('hsla(0, 0%, 55%, 0.22)');
  });

  it('skips tiles that belong to no block', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawPowerDistrictColors([{ x: 0, y: 0, track_bits: 1 }], [{ district_id: 'D1', block_ids: [] }]);
    expect(drawGridSegment).not.toHaveBeenCalled();
  });

  it('tolerates an undefined district list (every block reads ungrouped)', () => {
    const ctx = makeCtx();
    const drawGridSegment = vi.fn();
    const h = new RendererBlockHandler(makeRenderer(ctx, drawGridSegment));
    h.drawPowerDistrictColors([{ x: 0, y: 0, block_section_id: 'BLK_A', track_bits: 1 }], undefined);
    expect(drawGridSegment.mock.calls[0][5]).toBe('hsla(0, 0%, 55%, 0.22)');
  });
});
