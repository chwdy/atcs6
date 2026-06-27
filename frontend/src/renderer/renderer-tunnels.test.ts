import { describe, it, expect, vi } from 'vitest';

import { getCanvasPalette } from '../lib/canvas-palette';
import { RendererTunnelHandler, type TunnelHandlerRenderer } from './renderer-tunnels';

const pal = getCanvasPalette();

// A recording 2D-context stub and a minimal renderer back-reference, then we
// assert the observable draw calls. A tunnel mouth is now a "stud" (a bar across
// the rail) plus a small label — not a ring — so the assertions read off
// moveTo/lineTo, stroke widths, and setLineDash (which only the link uses).

function makeCtx() {
  const ctx: any = {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    arc: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    fillText: vi.fn(),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
    // recordings
    fillColors: [] as string[],
    dashCalls: [] as unknown[],
  };
  // Capture the fillStyle at each shape fill (the triangle color), and every
  // dash pattern set (only the connecting link sets one).
  ctx.fill = vi.fn(() => ctx.fillColors.push(ctx.fillStyle));
  ctx.stroke = vi.fn();
  ctx.setLineDash = vi.fn((arr: unknown) => ctx.dashCalls.push(arr));
  return ctx;
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): TunnelHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    // Simple, invertible projections so positions are easy to assert.
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    trackBitsAt: () => 1, // any bits — the stud centre doesn't depend on them
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
  ctx.fillText.mock.calls.map((call: unknown[]) => call[0]);
// Links are the only thing that sets a dash pattern.
const linkCount = (ctx: ReturnType<typeof makeCtx>) =>
  ctx.dashCalls.filter((a: unknown) => Array.isArray(a) && a.length > 0).length;

describe('RendererTunnelHandler.drawTunnels', () => {
  it('draws a label on each mouth, "T1", "T2", ...', () => {
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

  it('draws a filled triangle (not a ring) for each mouth', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels([{ a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } }], null);
    expect(ctx.arc).not.toHaveBeenCalled(); // no circular badge anymore
    expect(ctx.closePath).toHaveBeenCalledTimes(2); // two closed triangles
    expect(ctx.fillColors).toEqual([pal.accent, pal.accent]); // both filled accent
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

  it('bases the triangle a quarter-tile out from the tile centre toward the edge', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    // tile (2,4) -> centre (20,40); tileSize 40 -> quarter 10; edge 'E' -> (+,+).
    handler.drawTunnels([], { x: 2, y: 4, edge: 'E' });
    // The triangle base is the first moveTo->lineTo; its midpoint is the bar
    // centre, a quarter-tile out from the tile centre: (30,50).
    const mx = (ctx.moveTo.mock.calls[0][0] + ctx.lineTo.mock.calls[0][0]) / 2;
    const my = (ctx.moveTo.mock.calls[0][1] + ctx.lineTo.mock.calls[0][1]) / 2;
    expect([mx, my]).toEqual([30, 50]);
  });
});

describe('RendererTunnelHandler.tunnelAt', () => {
  const pair = { a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } };

  it('resolves the pair under either mouth, null elsewhere', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    // After a draw, mouth A sits at (0,-1) and mouth B at (50,51).
    handler.drawTunnels([pair], null);
    expect(handler.tunnelAt(0, -1)).toBe(pair);
    expect(handler.tunnelAt(50, 51)).toBe(pair);
    expect(handler.tunnelAt(200, 200)).toBeNull();
  });

  it('returns null before any draw (empty registry)', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    expect(handler.tunnelAt(0, -1)).toBeNull();
  });

  it('drops stale targets when a pair is no longer drawn', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels([pair], null);
    expect(handler.tunnelAt(0, -1)).toBe(pair);
    handler.drawTunnels([], null);
    expect(handler.tunnelAt(0, -1)).toBeNull();
  });
});

describe('RendererTunnelHandler.tunnelMouthAt', () => {
  const pair = { a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } };
  // After a draw, mouth A sits at (0,-1) and mouth B at (50,51).

  it('reports the clicked mouth and its partner', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels([pair], null);

    const nearA = handler.tunnelMouthAt(0, -1);
    expect(nearA?.clicked).toBe(pair.a);
    expect(nearA?.partner).toBe(pair.b);

    const nearB = handler.tunnelMouthAt(50, 51);
    expect(nearB?.clicked).toBe(pair.b);
    expect(nearB?.partner).toBe(pair.a);
  });

  it('returns null when no mouth is under the point', () => {
    const ctx = makeCtx();
    const handler = new RendererTunnelHandler(makeRenderer(ctx));
    handler.drawTunnels([pair], null);
    expect(handler.tunnelMouthAt(200, 200)).toBeNull();
  });
});

describe('RendererTunnelHandler selected highlight', () => {
  const pair = { a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } };

  it('fills the selected pair with the bright accent', () => {
    const ctx = makeCtx();
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair], null, { selected: pair });
    expect(ctx.fillColors).toEqual([pal.accentBright, pal.accentBright]);
  });

  it('fills an unselected pair with the plain accent', () => {
    const ctx = makeCtx();
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair], null);
    expect(ctx.fillColors).toEqual([pal.accent, pal.accent]);
  });

  it('matches the selected pair regardless of A/B order or edge case', () => {
    const ctx = makeCtx();
    const flipped = { a: { x: 5, y: 5, edge: 's' }, b: { x: 0, y: 0, edge: 'n' } };
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair], null, { selected: flipped });
    expect(ctx.fillColors).toEqual([pal.accentBright, pal.accentBright]);
  });
});

describe('RendererTunnelHandler connection lines', () => {
  const pair = { a: { x: 0, y: 0, edge: 'N' }, b: { x: 5, y: 5, edge: 'S' } };
  const other = { a: { x: 1, y: 1, edge: 'E' }, b: { x: 2, y: 2, edge: 'W' } };

  it('draws no link when nothing is selected and showLinks is off', () => {
    const ctx = makeCtx();
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair], null);
    expect(linkCount(ctx)).toBe(0);
  });

  it('draws the link for the selected pair', () => {
    const ctx = makeCtx();
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair], null, { selected: pair });
    expect(linkCount(ctx)).toBe(1);
  });

  it('shows every pair link under the debug toggle', () => {
    const ctx = makeCtx();
    new RendererTunnelHandler(makeRenderer(ctx)).drawTunnels([pair, other], null, {
      showLinks: true,
    });
    expect(linkCount(ctx)).toBe(2);
  });
});
