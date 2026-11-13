import { describe, expect, it, vi } from 'vitest';
import type { RunningTrain } from '../next/operate/live';
import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';
import { getCanvasPalette } from '../lib/canvas-palette';
import { RunningTrainOverlay, trainLineColor } from './running-train-overlay';

describe('v2 trains on the layout', () => {
  it('draws retained blocks and the claim ahead, and turns the tag\'s point when reversed', () => {
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
      setLineDash: vi.fn(), closePath: vi.fn(),
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
    // The tag is centred on the middle tile of the front block, (20, 20), and
    // its point leaves the 34 x 18 tag along the line of travel: 10 px past
    // the tag's edge, which a 45-degree line meets 9 / sin 45 from the centre.
    const reach = 9 / Math.SQRT1_2 + 10;
    const reaches = (x: number, y: number) => ctx.lineTo.mock.calls.some(
      ([px, py]) => Math.abs(px - x) < 1e-6 && Math.abs(py - y) < 1e-6,
    );
    expect(renderer.labelBounds[renderer.labelBounds.length - 1]).toMatchObject({ x: 3, y: 11, w: 34, h: 18 });
    expect(reaches(20 + reach * Math.SQRT1_2, 20 + reach * Math.SQRT1_2)).toBe(true);
    // Block A's piece of line starts set back from the block's end at
    // (-10, -10), by 0.7 of the track width (4 px at scale 1).
    const setBack = -10 + 2.8 * Math.SQRT1_2;
    expect(ctx.moveTo.mock.calls.some(
      ([x, y]) => Math.abs(x - setBack) < 1e-6 && Math.abs(y - setBack) < 1e-6,
    )).toBe(true);
    // The line carries on into the claimed block C, (30, 30) to (50, 50), and
    // its arrowhead's tip is the middle of that block.
    expect(reaches(40, 40)).toBe(true);
    expect(overlay.trainAt(0, 0, false)).toBe('T1');
    expect(overlay.trainAt(40, 40, false)).toBeUndefined();
    expect(overlay.trainAt(0, 0, true)).toBeUndefined();

    train.blocks.reverse();
    train.direction = 'reverse';
    ctx.lineTo.mockClear();
    overlay.draw('afterBlockDirection', network, renderer);
    expect(reaches(-reach * Math.SQRT1_2, -reach * Math.SQRT1_2)).toBe(true);

    // A lost train's tag has no point.
    train.lost = true;
    ctx.lineTo.mockClear();
    overlay.draw('afterBlockDirection', network, renderer);
    expect(reaches(-reach * Math.SQRT1_2, -reach * Math.SQRT1_2)).toBe(false);
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
