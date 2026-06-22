import { describe, expect, it, vi } from 'vitest';

import { Renderer } from './renderer';
import { SimRendererOverlay } from './sim-overlay';

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

    const net = { tracks: [], trains: [] };
    emit(r, 'afterStations', net);

    expect(overlay.draw).toHaveBeenCalledTimes(1);
    expect(overlay.draw).toHaveBeenCalledWith('afterStations', net, r);
  });

  it('_drawLive with no overlay draws the layout and pulls in no sim/stateManager', () => {
    const liveThis: any = {
      liveCtx: {},
      labelBounds: [],
      _overlays: [],
      _emitOverlay: Renderer.prototype._emitOverlay,
      _clearLiveCanvas: vi.fn(),
      platformHandler: { drawPlatforms: vi.fn() },
      stationHandler: { drawStations: vi.fn(), drawHoverOverlay: vi.fn() },
      drawBlockDirectionOverlay: vi.fn(),
      drawTileDirectionDebug: vi.fn(),
      drawOneWayDirectionDebug: vi.fn(),
      drawBoxSelection: vi.fn(),
      drawBoxSelectRect: vi.fn(),
      drawLassoPath: vi.fn(),
      _drawBuildPreview: vi.fn(),
      _drawHoverPreview: vi.fn(),
    };

    // A layout-only payload: no trains, no routes. With no overlay registered,
    // _drawLive must paint the layout and never touch trains/snapshot/stateManager.
    expect(() => Renderer.prototype._drawLive.call(liveThis, { tracks: [] })).not.toThrow();
    expect(liveThis.platformHandler.drawPlatforms).toHaveBeenCalledTimes(1);
    expect(liveThis.stationHandler.drawStations).toHaveBeenCalledTimes(1);
  });
});

describe('SimRendererOverlay', () => {
  function fakeRenderer(over: Record<string, unknown> = {}): any {
    return {
      routeHandler: { drawRouteOverlays: vi.fn() },
      trainHandler: { drawTrain: vi.fn() },
      _drawTrainGhost: vi.fn(),
      currentSnapshot: undefined,
      ...over,
    };
  }

  it('afterStations draws the route overlays', () => {
    const r = fakeRenderer();
    new SimRendererOverlay(null).draw('afterStations', { tracks: [] }, r);
    expect(r.routeHandler.drawRouteOverlays).toHaveBeenCalledTimes(1);
  });

  it('afterBlockDirection builds the snapshot and draws each train', () => {
    const r = fakeRenderer();
    const sm = { getController: vi.fn(() => null), getDispatcherProgress: vi.fn(() => null) };
    const net = { tracks: [], trains: [{ id: 't1' }, { id: 't2' }] };

    new SimRendererOverlay(sm).draw('afterBlockDirection', net, r);

    // Snapshot is built (no controllers → empty trains map) and stored.
    expect(r.currentSnapshot).toEqual({ trains: {} });
    expect(r.trainHandler.drawTrain).toHaveBeenCalledTimes(2);
  });

  it('top draws the train-placement ghost', () => {
    const r = fakeRenderer();
    new SimRendererOverlay(null).draw('top', { tracks: [] }, r);
    expect(r._drawTrainGhost).toHaveBeenCalledTimes(1);
  });

  it('draws no trains for a layout-only payload (no trains field)', () => {
    const r = fakeRenderer();
    new SimRendererOverlay(null).draw('afterBlockDirection', { tracks: [] }, r);
    expect(r.trainHandler.drawTrain).not.toHaveBeenCalled();
  });
});
