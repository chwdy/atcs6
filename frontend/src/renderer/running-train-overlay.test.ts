import { describe, expect, it, vi } from 'vitest';
import type { RunningTrain } from '../next/operate/live';
import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';
import { RunningTrainOverlay } from './running-train-overlay';

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
      fill: vi.fn(), fillRect: vi.fn(), strokeRect: vi.fn(), fillText: vi.fn(),
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
    expect(ctx.translate).toHaveBeenLastCalledWith(30, 30);
    expect(overlay.trainAt(0, 0, false)).toBe('T1');
    expect(overlay.trainAt(40, 40, false)).toBeUndefined();
    expect(overlay.trainAt(0, 0, true)).toBeUndefined();

    train.blocks.reverse();
    train.direction = 'reverse';
    overlay.draw('afterBlockDirection', network, renderer);
    expect(ctx.translate).toHaveBeenLastCalledWith(-10, -10);

    train.lost = true;
    ctx.translate.mockClear();
    overlay.draw('afterBlockDirection', network, renderer);
    expect(ctx.translate).not.toHaveBeenCalled();
    expect(ctx.fillText).toHaveBeenLastCalledWith('T1 ?', expect.any(Number), expect.any(Number));
  });
});
