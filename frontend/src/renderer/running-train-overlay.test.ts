import { describe, expect, it, vi } from 'vitest';
import type { RunningTrain } from '../next/operate/live';
import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';
import { getCanvasPalette } from '../lib/canvas-palette';
import { TRACK_LINE_WIDTH, TRACK_WIDTH } from '../lib/constants';
import { awayFrom, RunningTrainOverlay, tagSide, trainTagColor } from './running-train-overlay';

describe('v2 trains on the layout', () => {
  it('draws a border round retained blocks and into the claim ahead, with a chevron and a tag beside it', () => {
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
      setLineDash: vi.fn(), closePath: vi.fn(), arcTo: vi.fn(),
      fill: vi.fn(), roundRect: vi.fn(), fillText: vi.fn(),
    };
    const renderer = {
      ctx, gridSize: 40, scale: 1, labelBounds: [] as Array<{ x: number; y: number; w: number; h: number }>,
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
    const reaches = (x: number, y: number) => ctx.lineTo.mock.calls.some(
      ([px, py]) => Math.abs(px - x) < 1e-6 && Math.abs(py - y) < 1e-6,
    );
    const pointsAt = (x: number, y: number) => ctx.arcTo.mock.calls.some(
      ([px, py, , , r]) => r === 0 && Math.abs(px - x) < 1e-6 && Math.abs(py - y) < 1e-6,
    );
    // The chevron sits on the middle tile of the front block, centred on
    // (20, 20). The train runs south-east on screen. The chevron reaches 0.8
    // of the track's width to each side, and its tip is a little ahead of
    // the centre.
    const span = TRACK_LINE_WIDTH * 0.8;
    const ahead = span * 0.35 * Math.SQRT1_2;
    expect(reaches(20 + ahead, 20 + ahead)).toBe(true);
    // The 34 x 18 tag sits beside the chevron, off the track, on the upper
    // side: up and to the right of a track that runs south-east.
    const reach = (span + 5 + (34 / 2 + 18 / 2) * Math.SQRT1_2) * Math.SQRT1_2;
    const label = renderer.labelBounds[renderer.labelBounds.length - 1];
    expect(label).toMatchObject({ w: 34, h: 18 });
    expect(label.x).toBeCloseTo(20 + reach - 17);
    expect(label.y).toBeCloseTo(20 - reach - 9);
    // The tag's bottom-right corner is drawn out to a point 6.75 px further
    // on, the way the train faces.
    expect(pointsAt(label.x + 34 + 6.75, label.y + 18 + 6.75)).toBe(true);
    // The border starts set back from the end of block A at (-10, -10), by
    // 0.7 of TRACK_WIDTH.
    const setBack = -10 + TRACK_WIDTH * 0.7 * Math.SQRT1_2;
    expect(ctx.moveTo.mock.calls.some(
      ([x, y]) => Math.abs(x - setBack) < 1e-6 && Math.abs(y - setBack) < 1e-6,
    )).toBe(true);
    // The border carries on into the claimed block C, (30, 30) to (50, 50),
    // and stops at the middle of that block. A second chevron closes it,
    // with its tip 0.73 of the track's width further on.
    expect(reaches(40, 40)).toBe(true);
    const beyond = TRACK_LINE_WIDTH * 0.73 * Math.SQRT1_2;
    expect(reaches(40 + beyond, 40 + beyond)).toBe(true);
    expect(overlay.trainAt(0, 0, false)).toBe('T1');
    expect(overlay.trainAt(40, 40, false)).toBeUndefined();
    expect(overlay.trainAt(0, 0, true)).toBeUndefined();

    train.blocks.reverse();
    train.direction = 'reverse';
    // Reversed, the chevron sits on block A at (0, 0) and the tag is on the
    // same upper side of the track. The tag points north-west from its
    // top-left corner.
    ctx.arcTo.mockClear();
    renderer.labelBounds.length = 0;
    overlay.draw('afterBlockDirection', network, renderer);
    const reversed = renderer.labelBounds[renderer.labelBounds.length - 1];
    expect(reversed.x).toBeCloseTo(reach - 17);
    expect(reversed.y).toBeCloseTo(-reach - 9);
    expect(pointsAt(reversed.x - 6.75, reversed.y - 6.75)).toBe(true);

    // A lost train has no chevron and its tag has no point.
    train.lost = true;
    ctx.arcTo.mockClear();
    ctx.lineTo.mockClear();
    overlay.draw('afterBlockDirection', network, renderer);
    expect(reaches(-ahead, -ahead)).toBe(false);
    expect(ctx.arcTo.mock.calls.some(([, , , , r]) => r === 0)).toBe(false);
    expect(ctx.fillText).toHaveBeenLastCalledWith('T1 ?', expect.any(Number), expect.any(Number));
  });

  it("colours the tag by the train's state and keeps the line white with a dark edge", () => {
    const palette = getCanvasPalette();
    const colour = (state: RunningTrain['state'], lost = false) =>
      trainTagColor({ state, lost } as RunningTrain, palette);
    expect(colour('moving')).toBe(palette.trainTagMoving);
    expect(colour('waiting')).toBe(palette.trainTagWaiting);
    expect(colour('stopped')).toBe(palette.trainTagStopped);
    expect(colour('lost', true)).toBe(palette.trainTagLost);
    expect(new Set([colour('moving'), colour('waiting'), colour('stopped'), colour('lost', true)]).size).toBe(4);
    expect(palette.trainLine).toBe('#ffffff');
    expect(palette.trainLineEdge).not.toBe(palette.trainLine);
  });

  it('puts the tag on the upper side of the track, and to the right of a vertical stretch', () => {
    expect(tagSide(0)).toEqual({ x: 0, y: -1 });
    const west = tagSide(Math.PI);
    expect(west.x).toBeCloseTo(0);
    expect(west.y).toBeCloseTo(-1);
    expect(tagSide(Math.PI / 2)).toEqual({ x: 1, y: 0 });
    expect(tagSide(-Math.PI / 2)).toEqual({ x: 1, y: 0 });
  });

  it('takes an end\'s direction from the nearest point that is not the end itself', () => {
    // A cut that lands on a tile corner repeats the corner point. Two equal
    // points give no direction, and the arrowhead used to turn sideways.
    const line = [{ x: 0, y: 60 }, { x: 0, y: 40 }, { x: 0, y: 20 }, { x: 0, y: 20 }];
    expect(awayFrom(line, 3, -1)).toEqual({ x: 0, y: 40 });
    expect(awayFrom([{ x: 5, y: 5 }, { x: 5, y: 5.2 }, { x: 9, y: 5 }], 0, 1)).toEqual({ x: 9, y: 5 });
  });
});
