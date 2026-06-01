import { afterEach, describe, expect, it, vi } from 'vitest';

import { RendererRouteHandler, type RouteHandlerRenderer } from './renderer-routes';

// renderer-routes.js had no test before the migration; this is fresh coverage.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fillRect: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): RouteHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentNetwork: {},
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    requestRedraw: vi.fn(),
  };
}

afterEach(() => {
  delete (globalThis as { requestAnimationFrame?: unknown }).requestAnimationFrame;
  vi.restoreAllMocks();
});

describe('RendererRouteHandler', () => {
  it('fills and strokes a diamond per reachable tile', () => {
    const ctx = makeCtx();
    const h = new RendererRouteHandler(makeRenderer(ctx));
    h.setReachableHighlights(new Set(['2,3']));
    h.drawRouteOverlays();
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.stroke).toHaveBeenCalledTimes(1);
  });

  it('drawRoutePath fills each path tile', () => {
    const ctx = makeCtx();
    const h = new RendererRouteHandler(makeRenderer(ctx));
    h.drawRoutePath({ route_id: 'R1', status: 'active', path_tiles: [[0, 0], [1, 0]] }, false);
    expect(ctx.fill).toHaveBeenCalledTimes(2);
    expect(ctx.stroke).not.toHaveBeenCalled();
  });

  it('emphasized route strokes borders and draws direction arrows', () => {
    const ctx = makeCtx();
    const h = new RendererRouteHandler(makeRenderer(ctx));
    h.drawRoutePath(
      { route_id: 'R1', status: 'set', path_tiles: [[0, 0]], tile_directions: { '0,0': 'N' } },
      true,
    );
    expect(ctx.stroke).toHaveBeenCalledTimes(1); // border
    expect(ctx.fill).toHaveBeenCalledTimes(2); // tile fill + arrow fill
  });

  it('highlightRoute records the id and requests a redraw; clear resets it', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    const h = new RendererRouteHandler(r);
    h.highlightRoute({ route_id: 'R7' });
    expect(h.highlightedRouteId).toBe('R7');
    expect(r.requestRedraw).toHaveBeenCalled();
    h.clearRouteHighlight();
    expect(h.highlightedRouteId).toBeNull();
  });

  it('flashConflictTiles records tiles and kicks off the flash loop', () => {
    const raf = vi.fn();
    (globalThis as { requestAnimationFrame?: unknown }).requestAnimationFrame = raf;
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    const h = new RendererRouteHandler(r);
    h.flashConflictTiles([[1, 1]], 2000);
    expect(h.conflictFlashTiles).toEqual([[1, 1]]);
    expect(h.conflictFlashEnd).toBeGreaterThan(0);
    expect(r.requestRedraw).toHaveBeenCalled();
    expect(raf).toHaveBeenCalled();
  });
});
