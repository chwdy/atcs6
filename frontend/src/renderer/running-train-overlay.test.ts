import { describe, expect, it, vi } from 'vitest';
import type { RunningTrain } from '../next/operate/live';
import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';
import { getCanvasPalette } from '../lib/canvas-palette';
import { RunningTrainOverlay, trainLineColor } from './running-train-overlay';

describe('v2 trains on the layout', () => {
  it('draws retained blocks but not claims, and moves the arrow when reversed', () => {
    const train: RunningTrain = {
      train_id: 'T1', name: 'T1', direction: 'forward', knob_volts: 0,
      lost: false, state: 'stopped', waiting_reason: null, next_block_id: 'C',
      claimed_block_id: 'C', blocks: [
        { block_id: 'A', reads_occupied: false },
        { block_id: 'B', reads_occupied: true },
      ],
    };
    const ctx = {
      save: vi.fn(), restore: vi.fn(), measureText: () => ({ width: 20 }),
      beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
      setLineDash: vi.fn(), translate: vi.fn(), rotate: vi.fn(), closePath: vi.fn(),
      fill: vi.fn(), roundRect: vi.fn(), fillText: vi.fn(),
    };
    const renderer = {
      ctx, gridSize: 40, scale: 1, labelBounds: [],
      gridToScreenX: (x: number) => x * 20,
      gridToScreenY: (x: number) => x * 20,
      trackHandler: { getEdgePoints: () => ({ E: { x: 10, y: 10 }, W: { x: -10, y: -10 } }) },
      trackMap: {
        '0,0': { track_bits: 2 },
        '1,0': { track_bits: 2 },
        '2,0': { track_bits: 2 },
      },
    } as unknown as Renderer;
    const network = { blocks: {
      A: { ordered_tiles: [[0, 0, 'W']] },
      B: { ordered_tiles: [[1, 0, 'W']] },
      C: { ordered_tiles: [[2, 0, 'W']] },
    } } as unknown as NetworkPayload;
    const overlay = new RunningTrainOverlay(() => ({ trains: [train], emphasized: null }));

    overlay.draw('afterBlockDirection', network, renderer);
    // The arrow is centred on the middle tile of the front block.
    const tip = 20 + 7 * Math.SQRT1_2;
    const lastTip = () => ctx.translate.mock.calls[ctx.translate.mock.calls.length - 1] as number[];
    expect(lastTip()[0]).toBeCloseTo(tip, 3);
    expect(lastTip()[1]).toBeCloseTo(tip, 3);
    expect(overlay.trainAt(0, 0, false)).toBe('T1');
    expect(overlay.trainAt(40, 40, false)).toBeUndefined();
    expect(overlay.trainAt(0, 0, true)).toBeUndefined();

    train.blocks.reverse();
    train.direction = 'reverse';
    overlay.draw('afterBlockDirection', network, renderer);
    expect(lastTip()[0]).toBeCloseTo(-7 * Math.SQRT1_2, 3);
    expect(lastTip()[1]).toBeCloseTo(-7 * Math.SQRT1_2, 3);

    train.lost = true;
    ctx.translate.mockClear();
    overlay.draw('afterBlockDirection', network, renderer);
    expect(ctx.translate).not.toHaveBeenCalled();
    expect(ctx.fillText).toHaveBeenLastCalledWith('T1 ?', expect.any(Number), expect.any(Number));
  });

  it("colours the line by the train's state", () => {
    const palette = getCanvasPalette();
    const colour = (state: RunningTrain['state'], lost = false) =>
      trainLineColor({ state, lost } as RunningTrain, palette);
    expect(colour('moving')).toBe(palette.trainLineMoving);
    expect(colour('waiting')).toBe(palette.trainLineWaiting);
    expect(colour('stopped')).toBe(palette.trainLineStopped);
    expect(colour('lost', true)).toBe(palette.trainLineLost);
    expect(new Set([colour('moving'), colour('waiting'), colour('stopped'), colour('lost', true)]).size).toBe(4);
  });
});
