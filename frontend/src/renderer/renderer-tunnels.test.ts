import { describe, it, expect, vi } from 'vitest';

import { RendererTunnelHandler, type TunnelHandlerRenderer } from './renderer-tunnels';

// renderer-tunnels.js had no test before the migration; this is fresh coverage
// for the canvas-drawing handler. We feed it a recording 2D-context stub and a
// minimal renderer back-reference, then assert the observable draw calls.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    setLineDash: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    translate: vi.fn(),
    fillText: vi.fn(),
    // Settable style properties the handler writes to.
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): TunnelHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    // Simple, invertible projections so positions are easy to assert.
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    trackHandler: {
      getEdgePoints: () => ({
        N: { x: 0, y: -1 },
        S: { x: 0, y: 1 },
        E: { x: 1, y: 0 },
        W: { x: -1, y: 0 },
      }),
    },
  };
}

const labelsDrawn = (ctx: ReturnType<typeof makeCtx>) =>
  ctx.fillText.mock.calls.map((call) => call[0]);

describe('RendererTunnelHandler.drawTunnels', () => {
  it('draws two mouths per tunnel pair, labelled T1, T2, ...', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels(
      [
        { a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } },
        { a: { x: 1, y: 1, edge: 'E' }, b: { x: 2, y: 2, edge: 'W' } },
      ],
      null,
    );
    expect(labelsDrawn(ctx)).toEqual(['T1', 'T1', 'T2', 'T2']);
  });

  it('does not draw pairing lines unless showLinks is set', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    const tunnels = [{ a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } }];

    handler.drawTunnels(tunnels, null);
    expect(ctx.moveTo).not.toHaveBeenCalled();

    handler.drawTunnels(tunnels, null, { showLinks: true });
    expect(ctx.moveTo).toHaveBeenCalledTimes(1);
    expect(ctx.lineTo).toHaveBeenCalledTimes(1);
  });

  it('draws a pending "?" mouth when a pending endpoint is given', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels([], { x: 3, y: 3, edge: 'E' });
    expect(labelsDrawn(ctx)).toEqual(['?']);
  });

  it('coerces a non-array tunnels argument to empty (no draws)', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels(undefined, null);
    expect(ctx.fillText).not.toHaveBeenCalled();
  });

  it('positions a mouth at the tile screen point plus the edge offset', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    // gridToScreen maps (x,y) -> (x*10, y*10); edge 'E' offset is {1,0}.
    handler.drawTunnels([], { x: 2, y: 4, edge: 'E' });
    expect(ctx.translate).toHaveBeenCalledWith(21, 40);
  });
});
