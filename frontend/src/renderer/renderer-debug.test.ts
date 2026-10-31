import { afterEach, describe, expect, it, vi } from 'vitest';

import { canvasLayers, LAYERS, type LayerId } from '../lib/canvas-layers';
import { RendererDebugHandler, type DebugHandlerRenderer } from './renderer-debug';

// renderer-debug.js had no test before the migration; this is fresh coverage.
// We feed the handler a recording 2D-context stub and a minimal renderer
// back-reference, and turn on the canvas layers each overlay test needs.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    fillText: vi.fn(),
    roundRect: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
    globalAlpha: 1,
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): DebugHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
  };
}

const labels = (ctx: ReturnType<typeof makeCtx>) => ctx.fillText.mock.calls.map((c) => c[0]);

// Turn on exactly the given canvas layers (lib/canvas-layers.ts).
function showLayers(...ids: LayerId[]) {
  for (const layer of LAYERS) canvasLayers.set(layer.id, ids.includes(layer.id));
}

afterEach(() => {
  for (const layer of LAYERS) canvasLayers.set(layer.id, layer.defaultOn);
});

describe('RendererDebugHandler.getTrackExits', () => {
  const handler = () => new RendererDebugHandler(makeRenderer(makeCtx()));

  it('returns the opposite edge for straight track', () => {
    expect(handler().getTrackExits({ x: 0, y: 0, track_bits: 1 }, 'N')).toEqual(['S']); // NS
    expect(handler().getTrackExits({ x: 0, y: 0, track_bits: 2 }, 'W')).toEqual(['E']); // EW
  });

  it('returns [] for a missing track or entry edge', () => {
    expect(handler().getTrackExits(null, 'N')).toEqual([]);
    expect(handler().getTrackExits({ x: 0, y: 0, track_bits: 1 }, '')).toEqual([]);
  });

  it('picks the switch_state branch when a switch has multiple exits', () => {
    // NS + NE both present; entering 'N' yields exits ['S' (NS), 'E' (NE)].
    const track = { x: 0, y: 0, track_bits: 1 | 4, is_switch: true, switch_state: 1 };
    expect(handler().getTrackExits(track, 'N')).toEqual(['E']); // index 1
    expect(handler().getTrackExits({ ...track, switch_state: 0 }, 'N')).toEqual(['S']); // index 0
  });
});

describe('RendererDebugHandler.drawDebugOverlays', () => {
  it('draws nothing when all debug toggles are off', () => {
    showLayers();
    const ctx = makeCtx();
    new RendererDebugHandler(makeRenderer(ctx)).drawDebugOverlays([
      { x: 1, y: 2, track_bits: 5, is_switch: true, switch_state: 0 },
    ]);
    expect(ctx.fillText).not.toHaveBeenCalled();
  });

  it('draws coordinate labels for every track only when show-coords is on', () => {
    showLayers('coords');
    const ctx = makeCtx();
    new RendererDebugHandler(makeRenderer(ctx)).drawDebugOverlays([
      { x: 1, y: 2 },
      { x: 3, y: 4 },
    ]);
    expect(labels(ctx)).toEqual(['(1,2)', '(3,4)']);
  });

  it('draws the switch-state badge only for switch tiles when show-switches is on', () => {
    showLayers('switches');
    const ctx = makeCtx();
    new RendererDebugHandler(makeRenderer(ctx)).drawDebugOverlays([
      { x: 0, y: 0, is_switch: true, switch_state: 0 },
      { x: 1, y: 1, is_switch: false },
    ]);
    expect(labels(ctx)).toEqual(['MAIN']); // only the switch tile
    expect(ctx.roundRect).toHaveBeenCalledTimes(1);
  });
});

describe('RendererDebugHandler draw helpers', () => {
  it('drawEdgeLabels draws N / E / S / W', () => {
    const ctx = makeCtx();
    new RendererDebugHandler(makeRenderer(ctx)).drawEdgeLabels(0, 0);
    expect(labels(ctx)).toEqual(['N', 'E', 'S', 'W']);
  });

  it('drawBitVisualization plots one dot per bit slot', () => {
    const ctx = makeCtx();
    new RendererDebugHandler(makeRenderer(ctx)).drawBitVisualization(0, 0, 1);
    expect(ctx.arc).toHaveBeenCalledTimes(6);
  });
});
