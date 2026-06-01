import { afterEach, describe, expect, it, vi } from 'vitest';

import { RendererSignalHandler, type SignalHandlerRenderer } from './renderer-signals';

// renderer-signals.js had no test before the migration; this is fresh coverage
// for the canvas-drawing handler. We feed it a recording 2D-context stub, a
// minimal renderer back-reference, and stub the globals it reaches for
// (`document`, and `trackBitsToEdges` from the still-classic constants.js).

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    closePath: vi.fn(),
    setLineDash: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    fillText: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    font: '',
    textAlign: '',
    textBaseline: '',
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): SignalHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
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

const labels = (ctx: ReturnType<typeof makeCtx>) => ctx.fillText.mock.calls.map((c) => c[0]);

afterEach(() => {
  delete (globalThis as { document?: unknown }).document;
  delete (globalThis as { trackBitsToEdges?: unknown }).trackBitsToEdges;
  vi.restoreAllMocks();
});

describe('RendererSignalHandler.drawSingleSignal', () => {
  // drawSingleSignal reads document.getElementById('debug-show-signals').
  const installDoc = () => {
    (globalThis as { document?: unknown }).document = { getElementById: () => null };
  };

  it('bails out (and logs) when detection_direction is missing', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const ctx = makeCtx();
    new RendererSignalHandler(makeRenderer(ctx)).drawSingleSignal(
      { signal_id: 'S1', approach_direction: 'E' },
      2,
      20,
    );
    expect(errSpy).toHaveBeenCalled();
    expect(ctx.arc).not.toHaveBeenCalled();
  });

  it('draws a stick + lamp for a valid signal', () => {
    installDoc();
    const ctx = makeCtx();
    new RendererSignalHandler(makeRenderer(ctx)).drawSingleSignal(
      {
        signal_id: 'S1',
        approach_direction: 'E',
        detection_direction: 'W',
        state: 'green',
        signal_type: 'block',
        monitored_block_id: 'B1',
      },
      2,
      20,
    );
    // Base ring + lamp = two arcs; lamp fill once (no warning triangle).
    expect(ctx.arc).toHaveBeenCalledTimes(2);
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(labels(ctx)).toEqual([]); // no name label, no warning glyph
  });

  it('flags a block signal with no monitored block via a "!" triangle', () => {
    installDoc();
    const ctx = makeCtx();
    new RendererSignalHandler(makeRenderer(ctx)).drawSingleSignal(
      { signal_id: 'S1', approach_direction: 'E', detection_direction: 'W', signal_type: 'block' },
      2,
      20,
    );
    expect(labels(ctx)).toEqual(['!']);
  });
});

describe('RendererSignalHandler.drawSignalBlockConnections', () => {
  it('draws a dashed connector from a signal to its monitored block', () => {
    (globalThis as { trackBitsToEdges?: unknown }).trackBitsToEdges = () =>
      new Set(['N', 'S', 'E', 'W']);
    const ctx = makeCtx();
    new RendererSignalHandler(makeRenderer(ctx)).drawSignalBlockConnections([
      { x: 0, y: 0, track_bits: 1, block_section_id: 'B1' },
      {
        x: 5,
        y: 5,
        track_bits: 1,
        signals: [{ signal_id: 'S1', detection_direction: 'N', monitored_block_id: 'B1' }],
      },
    ]);
    expect(ctx.setLineDash).toHaveBeenCalledWith([4, 4]);
    expect(ctx.moveTo).toHaveBeenCalledTimes(1);
    expect(ctx.lineTo).toHaveBeenCalledTimes(1);
  });

  it('draws nothing when the monitored block has no tracks', () => {
    (globalThis as { trackBitsToEdges?: unknown }).trackBitsToEdges = () => new Set(['N']);
    const ctx = makeCtx();
    new RendererSignalHandler(makeRenderer(ctx)).drawSignalBlockConnections([
      {
        x: 5,
        y: 5,
        track_bits: 1,
        signals: [{ signal_id: 'S1', detection_direction: 'N', monitored_block_id: 'B-missing' }],
      },
    ]);
    expect(ctx.moveTo).not.toHaveBeenCalled();
  });
});
