import { describe, expect, it, vi } from 'vitest';

import { Renderer } from './renderer';

// Regression coverage for the editor build preview overlay. These used
// to be drawn imperatively right after requestRedraw(), but requestRedraw is
// rAF-coalesced: the deferred flush cleared the live canvas and wiped the
// overlay before the browser composited it, so the track tool's "potential
// tracks" never appeared. The fix moved the overlays into renderer state
// (setBuildPreview) painted by _drawBuildPreview during the _drawLive flush. These tests pin that dispatch
// without standing up the full canvas-backed Renderer (we call the paint
// methods on the prototype with a hand-built `this`).

function fakeRenderer(over: Record<string, unknown> = {}): any {
  return {
    buildPreview: null,
    trackHandler: {
      drawPreviewTracks: vi.fn(),
      drawEraserPreviewPath: vi.fn(),
      drawPreviewPath: vi.fn(),
    },
    segmentHandler: { drawSegmentPreviewPath: vi.fn() },
    ...over,
  };
}

const drawBuild = (r: any) => Renderer.prototype._drawBuildPreview.call(r);

describe('renderer build preview overlay', () => {
  it('paints nothing when no build preview is set', () => {
    const r = fakeRenderer();
    drawBuild(r);
    expect(r.trackHandler.drawPreviewTracks).not.toHaveBeenCalled();
    expect(r.trackHandler.drawPreviewPath).not.toHaveBeenCalled();
    expect(r.trackHandler.drawEraserPreviewPath).not.toHaveBeenCalled();
    expect(r.segmentHandler.drawSegmentPreviewPath).not.toHaveBeenCalled();
  });

  it('routes a track-tool preview to drawPreviewTracks (the regressed path)', () => {
    const tracks = [{ x: 0, y: 0, track_bits: 1 }];
    const r = fakeRenderer({ buildPreview: { kind: 'tracks', tracks } });
    drawBuild(r);
    expect(r.trackHandler.drawPreviewTracks).toHaveBeenCalledWith(tracks);
  });

  it('routes eraser / segment / path previews to their handlers', () => {
    const path = [{ x: 1, y: 2 }];

    const eraser = fakeRenderer({ buildPreview: { kind: 'eraser', path } });
    drawBuild(eraser);
    expect(eraser.trackHandler.drawEraserPreviewPath).toHaveBeenCalledWith(path);

    const segment = fakeRenderer({ buildPreview: { kind: 'segment', path } });
    drawBuild(segment);
    expect(segment.segmentHandler.drawSegmentPreviewPath).toHaveBeenCalledWith(path);

    const plain = fakeRenderer({ buildPreview: { kind: 'path', path } });
    drawBuild(plain);
    expect(plain.trackHandler.drawPreviewPath).toHaveBeenCalledWith(path);
  });
});

describe('_drawLive paints the previews as part of the live-layer flush', () => {
  // The actual regression: the live flush clears the canvas and repaints. If
  // _drawLive doesn't paint the previews, the editor's overlay is wiped every
  // frame. This drives the real _drawLive (siblings stubbed) and asserts the
  // preview dispatchers run inside it.
  it('invokes _drawBuildPreview', () => {
    const liveThis: any = {
      liveCtx: {},
      labelBounds: [],
      _clearLiveCanvas: vi.fn(),
      tunnelHandler: { drawTunnels: vi.fn() },
      highlightHandler: { draw: vi.fn() },
      // Running trains go through registered overlays, invoked via _emitOverlay.
      _emitOverlay: vi.fn(),
      drawBlockDirectionOverlay: vi.fn(),
      drawTileDirectionDebug: vi.fn(),
      drawOneWayDirectionDebug: vi.fn(),
      drawBoxSelection: vi.fn(),
      drawBoxSelectRect: vi.fn(),
      drawLassoPath: vi.fn(),
      _drawBuildPreview: vi.fn(),
    };
    Renderer.prototype._drawLive.call(liveThis, { tracks: [] });
    expect(liveThis._drawBuildPreview).toHaveBeenCalledTimes(1);
  });
});
