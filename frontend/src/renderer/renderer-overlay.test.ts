import { describe, expect, it, vi } from 'vitest';

import { Renderer } from './renderer';

// The overlay hook is exercised on the prototype with a hand-built `this`, the
// same idiom as renderer-preview.test.ts — no canvas-backed Renderer needed.
const emit = (r: any, phase: string, net: any) =>
  Renderer.prototype._emitOverlay.call(r, phase as any, net);

describe('renderer overlay hook', () => {
  it('_emitOverlay is a no-op when no overlay is registered', () => {
    const r = { _overlays: [] };
    expect(() => emit(r, 'top', { tracks: [] })).not.toThrow();
  });

  it('registerOverlay + _emitOverlay invokes each overlay with (phase, network, renderer)', () => {
    const overlay = { draw: vi.fn() };
    const r: any = { _overlays: [] };
    Renderer.prototype.registerOverlay.call(r, overlay);

    const net = { tracks: [] };
    emit(r, 'afterBlockDirection', net);

    expect(overlay.draw).toHaveBeenCalledTimes(1);
    expect(overlay.draw).toHaveBeenCalledWith('afterBlockDirection', net, r);
  });

  it('_drawLive with no overlay draws the layout without throwing', () => {
    const liveThis: any = {
      liveCtx: {},
      labelBounds: [],
      _overlays: [],
      _emitOverlay: Renderer.prototype._emitOverlay,
      _clearLiveCanvas: vi.fn(),
      tunnelHandler: { drawTunnels: vi.fn() },
      highlightHandler: { draw: vi.fn() },
      drawBlockDirectionOverlay: vi.fn(),
      drawTileDirectionDebug: vi.fn(),
      drawOneWayDirectionDebug: vi.fn(),
      drawBoxSelection: vi.fn(),
      drawBoxSelectRect: vi.fn(),
      drawLassoPath: vi.fn(),
      _drawBuildPreview: vi.fn(),
    };

    expect(() => Renderer.prototype._drawLive.call(liveThis, { tracks: [] })).not.toThrow();
    expect(liveThis.tunnelHandler.drawTunnels).toHaveBeenCalledTimes(1);
    expect(liveThis.highlightHandler.draw).toHaveBeenCalledTimes(1);
    expect(liveThis._drawBuildPreview).toHaveBeenCalledTimes(1);
  });
});
